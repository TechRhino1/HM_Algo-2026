"""
HM Algo 2.0 — Strict MT5 State Synchronization & Reconciliation Engine.
Guarantees real-time consistency between broker terminal state and internal application memory.
"""
import time
import logging
import threading
from typing import Dict, Set, Any, Optional

from jarvis.execution.mt5_client import MT5Client
from jarvis.application.state_manager import StateManager, GLOBAL_STATE
from jarvis.application.event_bus import EventBus, GLOBAL_EVENT_BUS

logger = logging.getLogger("JARVIS_StateSynchronizer")

class MT5StateSynchronizer:
    """Active background synchronizer and reconciler for MT5 broker states."""
    
    def __init__(
        self,
        mt5_client: MT5Client,
        state_manager: StateManager = GLOBAL_STATE,
        event_bus: EventBus = GLOBAL_EVENT_BUS,
        sync_interval_sec: float = 1.0
    ):
        self.mt5_client = mt5_client
        self.state_manager = state_manager
        self.event_bus = event_bus
        self.sync_interval_sec = sync_interval_sec
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_position_tickets: Set[int] = set()
        self._last_position_volumes: Dict[int, float] = {}
        self._backoff_delay = 1.0
        self._max_backoff = 30.0

    def start(self):
        if not self._running:
            self._running = True
            self._thread = threading.Thread(target=self._sync_loop, daemon=True, name="mt5_state_sync")
            self._thread.start()
            logger.info("MT5 State Synchronizer background worker started.")

    def stop(self):
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
            logger.info("MT5 State Synchronizer background worker stopped.")

    def sync_once(self) -> Dict[str, Any]:
        """Performs a single atomic synchronization pass and returns reconciliation report."""
        try:
            account = self.mt5_client.get_account_snapshot()
            positions = self.mt5_client.get_open_positions()

            self.state_manager.update_account(account)
            self.state_manager.update_positions(positions)

            # Reconcile closed/opened positions and partial fills
            current_positions_by_ticket = {p.ticket: p for p in positions}
            current_tickets = set(current_positions_by_ticket.keys())
            
            # Detect partial closes on surviving positions
            for t, p in current_positions_by_ticket.items():
                if t in self._last_position_volumes:
                    old_vol = self._last_position_volumes[t]
                    if p.volume < (old_vol - 1e-4):
                        closed_vol = round(old_vol - p.volume, 4)
                        logger.info(
                            f"State Reconciliation: Partial close detected on Position #{t} "
                            f"(volume {old_vol} -> {p.volume}, closed {closed_vol} lots)."
                        )
                        partial_deal_pnl = 0.0
                        exit_price = p.current_price
                        try:
                            import MetaTrader5 as mt5
                            with self.mt5_client._shared_lock:
                                deals = mt5.history_deals_get(position=t) if self.mt5_client.mode != "paper" else None
                            if deals:
                                exit_deals = [d for d in deals if getattr(d, "entry", 0) in (1, 3)]
                                if exit_deals:
                                    last_deal = exit_deals[-1]
                                    partial_deal_pnl = float(last_deal.profit + last_deal.swap + last_deal.commission)
                                    exit_price = float(last_deal.price)
                        except Exception as e:
                            logger.error(f"Failed to fetch deal history for partial close on position #{t}: {e}")

                        self.event_bus.publish_sync("POSITION_PARTIALLY_CLOSED", {
                            "ticket": t,
                            "symbol": p.symbol,
                            "remaining_volume": p.volume,
                            "closed_volume": closed_vol,
                            "realized_pnl": partial_deal_pnl,
                            "exit_price": exit_price,
                            "equity": account.equity
                        })

            # Detect newly closed positions
            closed_tickets = self._last_position_tickets - current_tickets
            for t in list(closed_tickets):
                try:
                    logger.info(f"State Reconciliation: Position #{t} closed on broker terminal.")
                    self.event_bus.publish_sync("POSITION_CLOSED", {"ticket": t})
                    self._last_position_volumes.pop(t, None)
                    
                    # Fetch deal history for ML feedback loop (§17)
                    try:
                        deals = None
                        if self.mt5_client.mode != "paper":
                            import MetaTrader5 as mt5
                            with self.mt5_client._shared_lock:
                                deals = mt5.history_deals_get(position=t)
                        if deals:
                            total_pnl = sum(d.profit + d.swap + d.commission for d in deals)
                            is_win = 1 if total_pnl > 0 else 0
                            symbol = deals[0].symbol if deals else "UNKNOWN"
                            exit_price = float(deals[-1].price) if len(deals) > 1 else float(deals[0].price)
                            self.event_bus.publish_sync('trade_closed', {
                                'ticket': t,
                                'symbol': symbol,
                                'pnl': total_pnl,
                                'is_win': is_win,
                                'exit_price': exit_price,
                                'equity': account.equity
                            })
                    except Exception as e:
                        logger.error(f"Failed to fetch deal history for closed position #{t}: {e}")
                except Exception as ex:
                    logger.error(f"Error handling closed ticket #{t}: {ex}")
                finally:
                    self._last_position_tickets.discard(t)

            # Detect newly opened positions
            opened_tickets = current_tickets - self._last_position_tickets
            for t in opened_tickets:
                logger.info(f"State Reconciliation: Position #{t} discovered on broker terminal.")
                self.event_bus.publish_sync("POSITION_OPENED", {"ticket": t})

            self._last_position_tickets = current_tickets
            self._last_position_volumes = {t: p.volume for t, p in current_positions_by_ticket.items()}

            status = "CONNECTED" if (self.mt5_client.is_connected and account.login > 0) else "DISCONNECTED"
            self.state_manager.update_service_health("MT5", status)
            self.state_manager.update_service_health("STATE_SYNC", "ONLINE")

            # Reset backoff on success
            self._backoff_delay = 1.0

            return {
                "success": True,
                "login": account.login,
                "equity": account.equity,
                "open_positions": len(positions),
                "closed_detected": len(closed_tickets),
                "opened_detected": len(opened_tickets)
            }
        except Exception as e:
            logger.error(f"State Synchronization error: {e}", exc_info=True)
            self.state_manager.update_service_health("STATE_SYNC", "DEGRADED")
            raise  # Re-raise to trigger backoff in sync_loop

    def _sync_loop(self):
        while self._running:
            try:
                self.sync_once()
                time.sleep(self.sync_interval_sec)
            except Exception:
                # Apply exponential backoff
                delay = self._backoff_delay
                logger.warning(f"Sync loop failed. Retrying in {delay}s...")
                time.sleep(delay)
                self._backoff_delay = min(delay * 2, self._max_backoff)

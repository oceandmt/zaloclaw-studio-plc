// Connection supervisor for the autobot listener.
//
// Problem it solves: zca-js's own `start({ retryOnClose: true })` only retries
// for close codes listed in the SERVER-provided `close_and_retry_codes`. A
// NORMAL_CLOSURE (1000) is NOT in that list, so when Zalo rotates/kicks the
// socket the listener stays dead forever while the process keeps running.
//
// This supervisor watches for `disconnected`/`closed`, then drives a full
// re-login + re-listen with exponential backoff, until it is explicitly
// stopped. It emits listen-* events so the panel reflects the true state.
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export function attachSupervisor({
  emit = () => {},
  getAccount = () => "",
  reconnect,
  intervalMs = 10000,
  graceMs = 60000,
  backoffBaseMs = 2000,
  backoffMaxMs = 60000,
  heartbeatMs = 45000,
} = {}) {
  if (typeof reconnect !== "function") throw new Error("attachSupervisor: reconnect fn required");
  const st = { connected: false, disconnectedAt: 0, stopped: false, reconnecting: false, attempts: 0, lastActivity: Date.now() };
  const acct = () => (typeof getAccount === "function" ? getAccount() : getAccount);

  function onConnected() {
    st.connected = true; st.disconnectedAt = 0; st.lastActivity = Date.now();
    emit({ event: "listen-connected", account: acct() });
  }
  function onDisconnected(code, reason) {
    st.connected = false; st.disconnectedAt = Date.now();
    emit({ event: "listen-disconnected", account: acct(), code, reason: String(reason || "") });
  }
  function noteActivity() { st.lastActivity = Date.now(); }

  async function tryReconnect() {
    if (st.reconnecting || st.stopped) return;
    st.reconnecting = true;
    try {
      while (!st.stopped) {
        st.attempts += 1;
        emit({ event: "listen-reconnect", account: acct(), attempt: st.attempts });
        try {
          await reconnect();
          st.attempts = 0; st.connected = true; st.disconnectedAt = 0;
          // NB: do NOT bump lastActivity here. A bare reconnect proves the
          // SOCKET is back, not that any message arrived. Keeping lastActivity
          // for message activity only lets the panel tell "socket alive" apart
          // from "quiet group" — the liveness signal is the heartbeat below.
          emit({ event: "listen-reconnected", account: acct() });
          return;
        } catch (e) {
          emit({ event: "listen-reconnect-error", account: acct(), attempt: st.attempts, error: String(e?.message || e) });
          if (st.stopped) return;
          await sleep(Math.min(backoffMaxMs, backoffBaseMs * st.attempts));
        }
      }
    } finally {
      st.reconnecting = false;
    }
  }

  const timer = setInterval(() => {
    if (st.stopped) return;
    if (st.connected && st.disconnectedAt && Date.now() - st.disconnectedAt > graceMs) {
      st.disconnectedAt = 0; // hand off to tryReconnect (it owns its own backoff)
      tryReconnect();
    }
  }, intervalMs);
  if (timer.unref) timer.unref();

  // Independent LIVENESS heartbeat. Previously the panel inferred "socket is
  // alive" from "last message seen", so a genuinely quiet source group looked
  // identical to a dead socket. This heartbeat ticks as long as the socket is
  // believed connected, giving the panel a real health signal to check against.
  const hb = setInterval(() => {
    if (st.stopped || !st.connected) return;
    emit({ event: "heartbeat", account: acct(), connected: true, lastActivity: st.lastActivity });
  }, heartbeatMs);
  if (hb.unref) hb.unref();

  return {
    onConnected,
    onDisconnected,
    noteActivity,
    tryReconnect,
    stop() { st.stopped = true; st.disconnectedAt = 0; clearInterval(timer); clearInterval(hb); },
    state: st,
  };
}

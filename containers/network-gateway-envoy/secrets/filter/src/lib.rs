//! Surf Scoter's secrets filter for the Envoy network gateway.
//!
//! Every instance runs in VMs with one `vm_id`, so they share Envoy's
//! process-wide shared data, across all workers and listeners.  That holds one
//! key, [`KEY`]: [`logic::PENDING`] until the secrets arrive, then the
//! secrets, which are never changed after that.
//!
//! * `{"mode": "inject"}`, on the inject listener (a 0600 Unix socket): takes
//!   one `POST /` of `{"values": {...}}`, and stores it, with compare-and-swap
//!   on the pending value, so only one delivery ever succeeds.  It answers
//!   with the names it took, never a value.
//! * `{"mode": "fill", "allow": [...]}`, on each read host's listener: fills
//!   `{{seekrit:NAME}}` in request header values with the secrets the host's
//!   rules name, refuses what it can't fill (403, or 503 until the delivery),
//!   and scrubs those values out of the response.  The names it filled go to
//!   filter state (`wasm.injected`), for the access log.

pub mod logic;

#[cfg(target_arch = "wasm32")]
mod glue;

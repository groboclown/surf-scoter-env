//! The proxy-wasm side: Envoy's calls in, and its host calls out.

use std::cell::RefCell;
use std::rc::Rc;

use proxy_wasm::traits::{Context, HttpContext, RootContext};
use proxy_wasm::types::{Action, ContextType, LogLevel, Status};

use crate::logic::{self, Mode, Refusal, Scrubber, Secrets};

/// The shared data's one key.
const KEY: &str = "surf-scoter-secrets";
/// Where the names filled go, for the access log: `%FILTER_STATE(wasm.injected:PLAIN)%`.
const INJECTED: &str = "injected";

proxy_wasm::main! {{
    proxy_wasm::set_log_level(LogLevel::Warn);
    proxy_wasm::set_root_context(|_| -> Box<dyn RootContext> { Box::new(Root::default()) });
}}

/// This VM's copy of the secrets, once they've arrived; shared by its
/// requests.  They never change after the delivery, so it's never refreshed.
type Cache = Rc<RefCell<Option<Rc<Secrets>>>>;

#[derive(Default)]
struct Root {
    mode: Option<Rc<Mode>>,
    cache: Cache,
}

impl Context for Root {}

impl RootContext for Root {
    fn on_vm_start(&mut self, _: usize) -> bool {
        // Mark the secrets pending, unless another VM already has (or they've
        // arrived): a delivery then swaps the pending value, once.
        if self.get_shared_data(KEY).0.is_none() {
            let _ = self.set_shared_data(KEY, Some(logic::PENDING), None);
        }
        true
    }

    fn on_configure(&mut self, _: usize) -> bool {
        // A configuration that's not one of ours stops Envoy from starting.
        self.mode = self
            .get_plugin_configuration()
            .as_deref()
            .and_then(logic::parse_mode)
            .map(Rc::new);
        self.mode.is_some()
    }

    fn get_type(&self) -> Option<ContextType> {
        Some(ContextType::HttpContext)
    }

    fn create_http_context(&self, _: u32) -> Option<Box<dyn HttpContext>> {
        match self.mode.as_deref()? {
            Mode::Inject => Some(Box::new(Inject)),
            Mode::Fill(_) => Some(Box::new(Fill {
                mode: self.mode.clone()?,
                cache: self.cache.clone(),
                scrubber: None,
            })),
        }
    }
}

fn reply(ctx: &dyn HttpContext, status: u32, text: &str) -> Action {
    ctx.send_http_response(
        status,
        vec![("content-type", "text/plain; charset=utf-8")],
        Some(text.as_bytes()),
    );
    Action::Pause
}

// ---------------------------------------------------------------------
// The delivery

struct Inject;

impl Context for Inject {}

impl HttpContext for Inject {
    fn on_http_request_headers(&mut self, _: usize, end_of_stream: bool) -> Action {
        if self.get_http_request_header(":method").as_deref() != Some("POST") {
            return reply(self, 405, "send the secrets with POST\n");
        }
        if self.get_http_request_header(":path").as_deref() != Some("/") {
            return reply(self, 404, "send the secrets to /\n");
        }
        if end_of_stream {
            return reply(self, 400, "no payload\n");
        }
        // Held here, so the router (whose only route is a 404) never sees
        // it; the filter answers once the body is in.
        Action::Pause
    }

    fn on_http_request_body(&mut self, size: usize, end_of_stream: bool) -> Action {
        if size > logic::MAX_PAYLOAD_BYTES {
            return reply(self, 413, "the payload is too large\n");
        }
        if !end_of_stream {
            // Buffered until it's all here.
            return Action::Pause;
        }
        let mut body = self.get_http_request_body(0, size).unwrap_or_default();
        let parsed = logic::parse_payload(&body);
        logic::zero(&mut body);
        let secrets = match parsed {
            Ok(secrets) => secrets,
            Err(why) => return reply(self, 400, &format!("{why}\n")),
        };
        let (current, cas) = self.get_shared_data(KEY);
        if current.as_deref().is_some_and(|c| c != logic::PENDING) {
            return reply(
                self,
                409,
                "the gateway already has its secrets; restart it to give them again\n",
            );
        }
        let mut data = logic::encode(&secrets);
        let stored = self.set_shared_data(KEY, Some(&data), cas);
        logic::zero(&mut data);
        match stored {
            Ok(()) => {
                let names: Vec<&str> = secrets.keys().map(String::as_str).collect();
                reply(
                    self,
                    200,
                    &format!("took {} secret(s): {}\n", names.len(), names.join(", ")),
                )
            }
            Err(Status::CasMismatch) => reply(
                self,
                409,
                "the gateway already has its secrets; restart it to give them again\n",
            ),
            Err(_) => reply(self, 500, "couldn't store the secrets\n"),
        }
    }
}

// ---------------------------------------------------------------------
// Filling in

struct Fill {
    mode: Rc<Mode>,
    cache: Cache,
    scrubber: Option<Scrubber>,
}

impl Context for Fill {}

impl Fill {
    fn allow(&self) -> &[String] {
        match &*self.mode {
            Mode::Fill(allow) => allow,
            Mode::Inject => &[],
        }
    }

    /// The secrets, once they've arrived.
    fn store(&self) -> Option<Rc<Secrets>> {
        if let Some(secrets) = self.cache.borrow().as_ref() {
            return Some(secrets.clone());
        }
        let mut data = self.get_shared_data(KEY).0?;
        let secrets = logic::decode(&data).map(Rc::new);
        logic::zero(&mut data);
        *self.cache.borrow_mut() = secrets.clone();
        secrets
    }

    fn refuse(&self, refusal: Refusal) -> Action {
        let (status, text) = refusal.response();
        self.set_property(vec!["refused"], Some(refusal.reason().as_bytes()));
        reply(self, status, &text)
    }
}

impl HttpContext for Fill {
    fn on_http_request_headers(&mut self, _: usize, _: bool) -> Action {
        let headers = self.get_http_request_headers();
        if !headers.iter().any(|(_, v)| v.contains(logic::PREFIX)) {
            return Action::Continue;
        }
        let store = self.store();
        let mut out: Vec<(String, String)> = Vec::with_capacity(headers.len());
        let mut names: Vec<String> = Vec::new();
        for (name, value) in headers {
            if !value.contains(logic::PREFIX) {
                out.push((name, value));
                continue;
            }
            if logic::is_unfillable_header(&name) {
                if logic::has_placeholder(&value) {
                    let where_ = if name.eq_ignore_ascii_case("host") {
                        "host"
                    } else {
                        &name[1..]
                    };
                    return self.refuse(Refusal::Misplaced(format!("the {where_}")));
                }
                out.push((name, value));
                continue;
            }
            match logic::fill(&value, self.allow(), store.as_deref()) {
                Ok(None) => out.push((name, value)),
                Ok(Some((filled, filled_names))) => {
                    out.push((name, filled));
                    for n in filled_names {
                        if !names.contains(&n) {
                            names.push(n);
                        }
                    }
                }
                Err(refusal) => return self.refuse(refusal),
            }
        }
        if names.is_empty() {
            return Action::Continue;
        }
        // An encoded response would hide the values from the scrubber: ask
        // for it as it is.
        out.retain(|(n, _)| !n.eq_ignore_ascii_case("accept-encoding"));
        self.set_http_request_headers(out.iter().map(|(n, v)| (n.as_str(), v.as_str())).collect());
        for (_, v) in &mut out {
            // SAFETY: zeroing a String's bytes leaves it valid UTF-8 (all NULs).
            logic::zero(unsafe { v.as_bytes_mut() });
        }
        self.set_property(vec![INJECTED], Some(names.join(",").as_bytes()));
        if let Some(store) = store {
            let injected: Vec<(String, String)> = names
                .iter()
                .filter_map(|n| store.get(n).map(|v| (n.clone(), v.clone())))
                .collect();
            self.scrubber = Scrubber::new(&injected);
        }
        Action::Continue
    }

    fn on_http_response_headers(&mut self, _: usize, _: bool) -> Action {
        let Some(scrubber) = &self.scrubber else {
            return Action::Continue;
        };
        let headers: Vec<(String, String)> = self
            .get_http_response_headers()
            .into_iter()
            // The body may change length as it's scrubbed.
            .filter(|(n, _)| !n.eq_ignore_ascii_case("content-length"))
            .map(|(n, v)| {
                let v = scrubber.header(&v).unwrap_or(v);
                (n, v)
            })
            .collect();
        self.set_http_response_headers(
            headers
                .iter()
                .map(|(n, v)| (n.as_str(), v.as_str()))
                .collect(),
        );
        Action::Continue
    }

    fn on_http_response_body(&mut self, size: usize, end_of_stream: bool) -> Action {
        if self.scrubber.is_none() {
            return Action::Continue;
        }
        let data = self.get_http_response_body(0, size).unwrap_or_default();
        let Some(scrubber) = &mut self.scrubber else {
            return Action::Continue;
        };
        let out = scrubber.chunk(&data, end_of_stream);
        if out != data {
            self.set_http_response_body(0, size, &out);
        }
        Action::Continue
    }
}

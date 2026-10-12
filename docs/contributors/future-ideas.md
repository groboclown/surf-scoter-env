# Grab Bag of Future Ideas

## Tool to do it all

Switch the `run-container.sh` script to be a Python script, and have it manage all the host executions and file manipulations.

I prefer keeping this as a script:

- It allows easier audit rather than running some random executable.  The `host` program already suffers from that, so maybe I could revisit this script decision.
- The script primarily runs other programs, so keeping it as a script makes it obvious the programs it runs.
- If someone uses alternate tools, they can muck with it easier than they can with a native executable.


## Full Network Intercept

Rather than using a squid proxy, this could use something like [mitmproxy](https://docs.mitmproxy.org/stable/) for full inspection and interactive proxying.  It adds complexity requiring sharing a root CA into the development environment.

Currently, without the HTTPS inspection, a container can allow `npm` and `pypi` and `github` uploads (and whatever else is in the allow list domains) - thus, they can become exfiltration channels.  Until the switch-over, this will remain an issue.

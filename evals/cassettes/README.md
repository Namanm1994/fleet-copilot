# Cassettes

Recorded model responses for `fleet eval --mode recorded` (offline, repeatable).

- One folder per case, one file per run: `<case_id>/run-<n>.json`.
- Each entry is keyed by call order and a digest of the request (messages and tool names).
  Only the model's response is stored; API keys and headers never are, and text is redacted.
- Record or refresh with `make eval-live` (`fleet eval --mode live --record`), which needs an
  API key in `.env`. Changing a prompt, a tool output or the data changes the request digest;
  the affected cases then report "cassette out of date" until they are re-recorded.

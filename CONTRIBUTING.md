# Contributing

Thanks for helping. The project is intentionally small: one Python file for the server, one for the MCP bridge, plain JS/CSS for the UI, **no runtime dependencies**.

```bash
python3 selftest.py > /dev/null && python3 test_security.py   # or: make test
python3 server.py --demo                                       # run locally with sample data
```

- Keep it dependency-free (standard library only) unless there is a strong reason.
- Every API behaviour needs a line in `selftest.py` (or `test_security.py` for security rules).
- UI changes: keep the page scroll model (no per-column scrollbars), work at phone width, and respect `prefers-color-scheme`.
- By contributing you agree your work is licensed under AGPL-3.0, like the rest of the project.

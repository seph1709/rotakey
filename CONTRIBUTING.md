# Contributing to RotaKey

Thanks for taking the time to contribute! Here's everything you need to get started.

---

## Ways to contribute

- **Bug reports** — open an issue with steps to reproduce
- **Feature requests** — open an issue describing the use case
- **Pull requests** — fixes, improvements, new provider support

---

## Getting started

```bash
git clone https://github.com/seph1709/rotakey
cd rotakey
pip install -r requirements-dev.txt
```

Run the test suite (no real API keys needed):

```bash
pytest tests/ -v
```

Lint:

```bash
ruff check proxy.py tests/
```

---

## Pull request checklist

- [ ] Tests pass (`pytest tests/ -v`)
- [ ] Lint passes (`ruff check proxy.py tests/`)
- [ ] New behaviour is covered by a test
- [ ] `rotakey.yaml` updated if config schema changed
- [ ] `README.md` updated if the user-facing behaviour changed

---

## Commit style

Use short, imperative commit messages:

```
fix: handle empty key list on startup
feat: add retry-after header support
docs: clarify Docker host binding
```

---

## Reporting security vulnerabilities

Do **not** open a public issue. See [SECURITY.md](SECURITY.md).

---

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).

# Soup experiment tracker

## Run

```sh
docker compose up --build
```

Create a project with `POST /api/projects`, then create runs with its returned
Bearer key. Open `/runs/{run_id}?key={api_key}` to see the live page.

## Design and verification

`DESIGN.md` describes ordering, replay, isolation, silent-failure checks, and
the shipment verdict. The focused tests are in `test_app.py`; run them with:

```sh
python -m pytest -q
```

AI was used to sketch the FastAPI/SQLite structure and test cases. I checked
the transaction boundaries, authentication paths, replay behavior, and
re-ran the tests after changing the implementation. I did not treat generated
code or passing tests as proof of production readiness.

What surprised me was how easily a UI can look correct while missing an event:
the canonical database read is more trustworthy than applying deltas forever.
I still worry about retention/backpressure and multi-process deployment; the
next version needs PostgreSQL, a bounded replay policy, and observability.

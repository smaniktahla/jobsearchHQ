"""Child-process entry point for a cancellable board scrape.

JobSpy's scrape_jobs() is a long blocking call with no cancel hook, so a search that
may need to be stopped runs it in a separate process that the parent can terminate.
Kept dependency-light on purpose: the child is started with 'spawn' and re-imports this module.
"""


def scrape(kwargs: dict, conn) -> None:
    try:
        from jobspy import scrape_jobs
        df = scrape_jobs(**kwargs)
        conn.send(("ok", df))
    except BaseException as exc:  # report everything, including SystemExit from a library
        try:
            conn.send(("err", f"{type(exc).__name__}: {exc}"))
        except Exception:
            pass
    finally:
        conn.close()


# Test doubles for the cancellable-scrape tests (spawned children can only run importable functions).
def _sleep_forever(kwargs: dict, conn) -> None:
    import time
    time.sleep(300)


def _return_rows(kwargs: dict, conn) -> None:
    import pandas as pd
    conn.send(("ok", pd.DataFrame([{"title": "T", "company": "C", "site": "indeed"}])))
    conn.close()

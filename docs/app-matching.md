# App-specific restore matching

Stale-session snapshot restore calls `restore_name_key(window)` from
[`app_matching.py`](../src/spacekeeper/app_matching.py) for saved and current
windows. The function case-folds app names and titles, then applies a registered
title rule. Unregistered apps keep their full titles. The planner requires a
unique key in both observations and binds the result to its current process
identity before any commands.

To add an app, write a pure title function in that file and add it to
`APP_TITLE_RULES` under the case-folded app name. For example, an app whose titles
are `Report | page 1` could use:

```python
def document_title(title: str) -> str:
    return title.rsplit(' | page ', 1)[0]


APP_TITLE_RULES = {
    # Existing rules stay here.
    'example viewer': document_title,
}
```

Rules receive case-folded titles and must return a stable title string. Keep
meaningful distinctions such as workspaces, remote hosts, or development hosts.
Register the same function for several app names when they share title syntax;
their app names still match separately. This is a source extension point, not a
runtime configuration option.

Add tests with invented titles for successful matches and duplicates in both
saved and current observations. `tests/test_app_matching.py` demonstrates a new
rule used by the real planner without modifying it. Run:

```sh
uv run --locked python -m unittest tests.test_app_matching tests.test_fallback -q
```

Use `uv run spacekeeper` to try changes from the checkout. To update a globally
installed command after editing rules, reinstall from the project directory:

```sh
uv tool install --reinstall --python 3.12 .
```

Title rules only affect snapshot name fallback. Same-session owner matching,
`--no-fallback`, preset selectors, preserve filters, and final verification retain
their existing policies. Rules cannot recover missing query metadata: an empty
title/role needs a valid observation before it can identify a saved document.

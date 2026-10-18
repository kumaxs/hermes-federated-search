# Running the tests

The repository ships a single pytest suite: **117 tests** (91 for the federated
search provider, 26 for the desktop widget — the widget tests spawn
`node tests/widget_harness.cjs` internally).

## Requirements

- **Python >= 3.10** (tested on 3.12).
- **Node.js on PATH** (the desktop widget tests run the widget harness in a
  `node` subprocess; tested on v22).
- **git + network access on first setup**: the tests import the hermes-agent
  SDK, which is not published on PyPI — it is fetched from GitHub at the
  pinned commit `7e6f1c4060619beba1619ee9fd385ef7d8e0ca0a`
  (`kumaxs/hermes-agent`, branch `feat/federated-search-config`).
- Test dependencies on PyPI: `pytest`, `ruamel.yaml` (see
  `requirements-dev.txt`).

## Setup

1. Clone the repository. **The checkout directory name must not contain a
   hyphen** (or any character that is not valid in a Python identifier):
   the repo root has an `__init__.py`, so pytest imports the checkout as a
   package; in a hyphenated directory every test errors with
   `ImportError: attempted relative import with no known parent package`.
   Use a name like `federated_search_plugin`:

   ```
   git clone https://github.com/kumaxs/hermes-federated-search.git ~/src/federated_search_plugin
   ```

2. Create a virtual environment and install the test dependencies:

   ```
   cd ~/src/federated_search_plugin
   python3.12 -m venv .venv
   . .venv/bin/activate
   pip install -r requirements-dev.txt
   ```

3. Fetch the hermes-agent SDK at the pinned commit:

   ```
   git init ~/src/hermes_agent_sdk
   cd ~/src/hermes_agent_sdk
   git remote add origin https://github.com/kumaxs/hermes-agent.git
   git fetch --depth 1 origin 7e6f1c4060619beba1619ee9fd385ef7d8e0ca0a
   git checkout FETCH_HEAD
   ```

4. Put the SDK on `PYTHONPATH` (the provider imports `agent.web_search_provider`
   from it):

   ```
   export PYTHONPATH=~/src/hermes_agent_sdk
   ```

## Run

From the checkout, with the venv active and `PYTHONPATH` set:

```
cd ~/src/federated_search_plugin
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests -q -p no:cacheprovider
```

The two flags keep the checkout clean: `PYTHONDONTWRITEBYTECODE=1` suppresses
`__pycache__/`, `-p no:cacheprovider` disables `.pytest_cache/`.

To run a single file:

```
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/test_federated_search.py -q -p no:cacheprovider
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/test_desktop_widget.py -q -p no:cacheprovider
```

## Expected output

```
117 passed in 2.65s
```

(The count grows as tests are added. A regression prints each failing test
with its traceback; an environment problem usually shows up as collection
errors or a batch of identical failure messages — see below.)

## Troubleshooting

- **Every test errors, traceback mentions
  `ImportError: attempted relative import with no known parent package`** —
  the checkout directory name contains a hyphen. The repo root is a package;
  pytest maps the directory name onto a module path and `from .provider import …`
  cannot resolve. Rename the directory (e.g. `federated_search_plugin`) and
  re-run. `--import-mode=importlib` does not help.
- **Collection aborts with `ModuleNotFoundError: No module named 'agent'`** —
  the hermes-agent SDK is not on `PYTHONPATH`. Do step 3–4 of Setup.
- **A batch of failures with `Federated search failed: No module named 'ruamel'`** —
  the test dependencies are not installed. Run
  `pip install -r requirements-dev.txt`.
- **The desktop widget tests fail with a `node`-related error** — Node.js is
  not on PATH. Install Node.js (v22 tested) and re-run.

## Continuous integration

The `tests` GitHub Actions workflow (`.github/workflows/tests.yml`) runs the
full suite on every push and on every pull request:

- Checks out this repository into a directory literally named `repo`
  (hyphen-free by construction — the CI equivalent of the rename advice
  above), and the pinned hermes-agent SDK into `sdk`.
- Installs `requirements-dev.txt`, then runs the command from the Run
  section, on `ubuntu-latest` with Python 3.12 and Node 22.

Where to see it: the repository's Actions tab → workflow `tests`, or the
checks block on any pull request. Green signal: the run is green and the log
ends with `117 passed`. Red signal: the run fails with the failing tests and
their summaries in the log.

To make it a merge gate, enable branch protection on the default branch and
require the `tests` status check (a one-time repository owner action).

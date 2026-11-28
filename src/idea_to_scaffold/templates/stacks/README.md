# Stack skeletons

`its scaffold` copies `stacks/<stack.language>/` into the new repo (falling back to `generic/`).

- `${var}` placeholders are filled from `package.yaml`: `name`, `slug`, `summary`, `module` (slug with `_`),
  `runtime`, `runtime_nodot`, `cmd_setup`, `cmd_test`, `cmd_lint`, `cmd_run`.
- A path segment named `__module__` is renamed to the module name, and a trailing `.tmpl` is dropped
  (use it for files like `pyproject.toml` that tools would otherwise try to parse).
- Add a new stack by adding a folder here. A `stack.language` of `javascript` or `js` uses `node/`.
- `node/` runs its tests with Node's built-in runner (`node --test`), whose results the done gate reads
  (`stacks.py`); keep the `test` script on it, or the gate has only the exit code to go on.

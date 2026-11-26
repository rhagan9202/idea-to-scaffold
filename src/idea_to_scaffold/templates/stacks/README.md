# Stack skeletons

`its scaffold` copies `stacks/<stack.language>/` into the new repo (falling back to `generic/`).

- `${var}` placeholders are filled from `package.yaml`: `name`, `slug`, `summary`, `module` (slug with `_`),
  `runtime`, `runtime_nodot`, `cmd_setup`, `cmd_test`, `cmd_lint`, `cmd_run`.
- A path segment named `__module__` is renamed to the module name, and a trailing `.tmpl` is dropped
  (use it for files like `pyproject.toml` that tools would otherwise try to parse).
- Add a new stack by adding a folder here.

# Stack skeletons

`its scaffold` copies `stacks/<stack.language>/` into the new repo (falling back to `generic/`).

- `${var}` placeholders are filled from `package.yaml`: `name`, `slug`, `summary`, `module` (slug with `_`),
  `runtime`, `runtime_nodot`, `cmd_setup`, `cmd_test`, `cmd_lint`, `cmd_run`.
- A path segment named `__module__` is renamed to the module name.
- Add a new stack by adding a folder here.

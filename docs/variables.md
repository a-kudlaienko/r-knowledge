# Variables — resolving `{{ var }}` and `${var.x}` edge targets

Reference for `knowledge vars`. The skill carries only the short version; this is the
full behaviour. See also [`docs/graph-visualization.md`](graph-visualization.md) and the
`relations` material in `skill-template/SKILL.md`.

Some edges (mostly Ansible `include_tasks`/`include_role` and Terraform
`templatefile`/`source`) carry template expressions like `_tasks/{{ deploy_env }}/...` or
`source = "./${var.env}"`. Without the variables, these edges show as `kind="parametric"`
with no `file` — the LLM can see *something* is there but not where it points.

## Setting variables

Per-project, scoped by domain:

```bash
knowledge vars set ansible deploy_env=prod region=us-east            # multi-kv
knowledge vars set terraform env=prod                                 # scoped separately
knowledge vars set all region=us-east-1                               # catch-all merged into any scope
knowledge vars import ansible /path/to/vars.json                      # bulk from JSON
knowledge vars list [--scope ansible] [--json]
knowledge vars unset ansible deploy_env                               # remove one
knowledge vars unset ansible --all                                    # clear a scope
knowledge vars unset --auto                                           # clear auto-loaded rows
```

Every mutation auto-applies against the existing graph — no rebuild needed.

## Ansible auto-load

`build`/`update` reads `group_vars/all*` and `host_vars/*` (project root, every
`ansible.cfg` dir, every `inventory =` dir) into `scope='ansible'` automatically.
Precedence per Ansible docs: inventory `group_vars/all` < playbook `group_vars/all` <
inventory `host_vars/*` < playbook `host_vars/*`. Manual `vars set` rows always beat auto
rows. `vars list` tags auto rows with `(auto:group_vars)` / `(auto:host_vars)`.

## Scope routing

| Edge kind | Syntax | Scope lookup order |
|---|---|---|
| `ansible_*` | Jinja `{{ name }}` | `ansible`, then `all` |
| `helm_*` | Jinja `{{ name }}` | `helm`, then `all` |
| `tf_*` | Terraform `${var.name}` | `terraform`, then `all` |

## Display kinds for NULL-target edges

- `parametric` — waiting for variables. Set them with `vars set`.
- `external` — resolved to not-a-project-file (stdlib / third-party / remote module source).
- `unresolved` — syntactically irrecoverable (e.g., `import_module(some_expr)` with a
  non-literal arg).

## Not substituted

Jinja filters (`{{ x | lower }}` → takes `x`, ignores filter), loop vars (`{{ item }}`,
`{{ role_item }}`), nested attrs (`{{ foo.bar }}`), arithmetic/expressions. Those stay
parametric by design — set a concrete value if you want them resolved.

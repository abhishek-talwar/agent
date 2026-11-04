# demo_project

A minimal fixture for trying Helix end to end.

`calc.divide` has an intentional bug: dividing by zero raises a raw
`ZeroDivisionError`. The task is to make it raise a clear `ValueError` instead.

Run Helix against this directory:

```bash
cd examples/demo_project
export HELIX_WORKSPACE=$PWD
helix serve
```

Then solve this problem in the **Solve** tab:

> `calc.py` raises ZeroDivisionError when the divisor is 0. It should raise a clear
> ValueError instead.

with these acceptance criteria:

- `contains` -> `calc.py::raise ValueError`
- `python` -> `import calc; assert calc.divide(6, 3) == 2`

Helix will propose the change as a reviewable diff; nothing is written until you
approve it, and `solved` is only true once the criteria pass.

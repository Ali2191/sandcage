"""The plan: a locked capability manifest made before anything untrusted is read, merged
with the rulebook into the run's effective policy (rulebook ∩ plan, strictest wins).

Modules: ``manifest`` (validation and clipping), ``risk`` (the harness's risk floor),
``layer`` (the plan as a policy layer, amendments), ``planner`` (the model call)."""

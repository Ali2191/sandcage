"""Everything physical about a run: the safe file layer, the workspace copy, the diff, the
container, the SSRF-safe fetcher, the runtime probe and the run object that glues the
policy gate to the tools.

Import the submodules directly (``sandcage.sandbox.safefs`` and so on); this package
re-exports nothing so that importing one piece never loads Docker or network code.
"""

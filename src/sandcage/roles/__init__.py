"""The reviewing roles around the agent: scout, understand, reader, verifier, red-team.

Each is a quarantined model call with no tools; every reply is untrusted input, parsed by
one validate-clip-cap-mask function per role, and every role fails closed."""

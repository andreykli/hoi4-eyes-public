"""Shared bounds for absolute dates emitted by the HOI4 Clausewitz engine."""

# `global.num_days` is an absolute day count, not days elapsed in this save.
# The live 1936-01-01 marker is 706640. Four million covers dates through
# year 10,000 while still rejecting malformed/unbounded values.
MAX_HOI4_ABSOLUTE_DAY = 4_000_000


pecli 1.0 - tiny key/value store
================================

Usage:
  pecli init <file>
  pecli add <file> <key> <value>
  pecli get <file> <key>
  pecli list <file>
  pecli remove <file> <key>
  pecli checksum <file>

sample.dat is a ready-made store with three records.

Exit codes: 0 ok, 1 usage or key not found, 2 bad magic, 3 file missing,
4 corrupt file (checksum mismatch), 5 write failure.

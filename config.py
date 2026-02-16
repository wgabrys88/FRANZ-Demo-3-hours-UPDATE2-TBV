"""
config.py — Hot-reloadable parameters for FRANZ.

main.py reloads this file every turn, so you can edit it
while the loop is running to adjust sampling, execution mode, etc.
"""

TEMPERATURE: float = 0.7
TOP_P: float = 0.9
MAX_TOKENS: int = 2048

# True = VLM can only call tool functions. False = full Python environment.
RESTRICTED_EXEC: bool = True

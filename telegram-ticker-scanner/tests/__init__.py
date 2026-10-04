import logging

# Keep test output readable: the code under test logs reconnections and errors on purpose.
logging.getLogger().setLevel(logging.CRITICAL)

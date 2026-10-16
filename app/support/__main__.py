"""Run bounded retention under the central support principal: python -m app.support."""

import os

from .store import SupportStore

if __name__ == "__main__":
    result = SupportStore(os.environ["SUPPORT_DATABASE_URL"]).prune()
    print(result)

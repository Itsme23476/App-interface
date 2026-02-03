import sys
import json
from pathlib import Path

# Make app modules importable when running from project root
sys.path.insert(0, 'app')

from core.search import search_service  # type: ignore
from core.database import file_index  # type: ignore


def main(argv: list[str]) -> int:
    # Simple CLI: index a directory, optionally clear index first and run a test search
    # Usage: python scripts/index_dir.py [--clear] [--search "query"] DIR
    clear = False
    query: str | None = None
    args = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == '--clear':
            clear = True
            i += 1
            continue
        if a == '--search' and i + 1 < len(argv):
            query = argv[i + 1]
            i += 2
            continue
        args.append(a)
        i += 1

    if not args:
        print('ERROR: Missing DIR. Usage: python scripts/index_dir.py [--clear] [--search "query"] DIR')
        return 2

    directory = Path(args[0])
    if not directory.exists() or not directory.is_dir():
        print(f'ERROR: Not a directory: {directory}')
        return 2

    if clear:
        file_index.clear_index()

    res = search_service.index_directory(directory)
    print('INDEX_RES=' + json.dumps(res))
    stats = search_service.get_index_statistics()
    print('STATS=' + json.dumps(stats))

    if query:
        results = search_service.search_files(query, limit=50)
        print(f'SEARCH[{query}] count={len(results)}')
        # print first few filenames
        for r in results[:10]:
            print(' -', r.get('file_name'), '| label=', r.get('label'), '| has_ocr=', r.get('has_ocr'))

    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))



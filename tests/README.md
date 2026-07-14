# OpenAver unit tests

## Run

```bat
OpenAver\python\python.exe tests\run_all.py
```

Or a single module:

```bat
OpenAver\python\python.exe -m unittest tests.test_field_meta -v
```

## Coverage (current)

| Module | What it guards |
|--------|----------------|
| `test_field_meta` | locks, sources, translation skip / confirm / force |
| `test_database_locks` | DB persist of locks / sources / translation_meta |
| `test_enricher_locks` | fill_missing merge respects field_locks |
| `test_path_utils` | Windows casefold path compare + boundary |
| `test_image_proxy` | Referer/UA map + disk cache hit/miss |
| `test_media_download_errors` | Chinese error classification |
| `test_rename_journal` | rename history append / rollback mark |
| `test_source_diagnostics` | deps + source status aggregation |
| `test_javdb_proxy` | curl_cffi proxies + geo-block handling |
| `test_cf_transport` | existing CF transport unit tests |
| `test_scan_diff` | incremental scan: mtime/size/nfo + casefold keys |
| `test_duplicates` | number/size groups + content fingerprint |
| `test_diagnostic_pack` | redacted zip export |
| `test_actress_alias_review` | unrecognized names + group merge |

Tests that touch SQLite use a temporary DB (`_helpers.TempDb`) and do **not** write to the live library.

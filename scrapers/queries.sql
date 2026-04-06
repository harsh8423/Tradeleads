-- queries.sql -- Useful queries for domains.db
-- Run with: sqlite3 E:\domains.db < queries.sql

-- =====================================================
-- 1. Category summary
-- =====================================================
-- SELECT COALESCE(category, 'uncategorized') as cat, COUNT(*) as cnt
-- FROM domains GROUP BY category ORDER BY cnt DESC;

-- =====================================================
-- 2. Find domains by keyword (partial match)
-- =====================================================
-- SELECT domain, category FROM domains
-- WHERE domain LIKE '%logistics%' LIMIT 50;

-- =====================================================
-- 3. Metadata: domains with titles containing a keyword
-- =====================================================
-- SELECT domain, category, title, description
-- FROM domain_metadata
-- WHERE title LIKE '%freight%' LIMIT 50;

-- =====================================================
-- 4. Metadata: all logistics domains that responded 200
-- =====================================================
-- SELECT domain, title, description, language
-- FROM domain_metadata
-- WHERE category = 'logistics' AND status_code = 200;

-- =====================================================
-- 5. Metadata: status code breakdown
-- =====================================================
-- SELECT status_code, COUNT(*) as cnt,
--        ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM domain_metadata), 1) as pct
-- FROM domain_metadata GROUP BY status_code ORDER BY cnt DESC;

-- =====================================================
-- 6. Metadata: domains by language
-- =====================================================
-- SELECT language, COUNT(*) as cnt
-- FROM domain_metadata WHERE language IS NOT NULL
-- GROUP BY language ORDER BY cnt DESC LIMIT 20;

-- =====================================================
-- 7. Metadata: export to CSV (run from command line)
-- =====================================================
-- sqlite3 -header -csv E:\domains.db \
--   "SELECT domain, category, title, description, language
--    FROM domain_metadata WHERE status_code=200 AND category='logistics'"
--   > logistics_200.csv

-- =====================================================
-- 8. Metadata: domains with location info
-- =====================================================
-- SELECT domain, category, title, location
-- FROM domain_metadata WHERE location IS NOT NULL LIMIT 50;

-- =====================================================
-- 9. Metadata: slowest domains (fetch time)
-- =====================================================
-- SELECT domain, fetch_ms, status_code, error_msg
-- FROM domain_metadata ORDER BY fetch_ms DESC LIMIT 20;

-- =====================================================
-- 10. Metadata: failed domains (for retry / analysis)
-- =====================================================
-- SELECT domain, category, status_code, error_msg
-- FROM domain_metadata WHERE status_code < 0
-- ORDER BY status_code LIMIT 100;

"""Diagnostic script for pipeline issues."""
import sqlite3
import json

conn = sqlite3.connect('jobs.db')
conn.row_factory = sqlite3.Row

# Total jobs
total = conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
print(f'Total jobs: {total}')

# Jobs from today
today_jobs = conn.execute("SELECT COUNT(*) FROM jobs WHERE date(first_seen) = date('now', 'localtime')").fetchone()[0]
print(f'Jobs first seen today: {today_jobs}')

# Jobs from last 3 days
recent_jobs = conn.execute("SELECT COUNT(*) FROM jobs WHERE first_seen >= datetime('now', '-3 days')").fetchone()[0]
print(f'Jobs first seen last 3 days: {recent_jobs}')

# Unscored jobs (haiku_score IS NULL)
unscored = conn.execute('SELECT COUNT(*) FROM jobs WHERE haiku_score IS NULL').fetchone()[0]
print(f'Unscored (no haiku_score): {unscored}')

# Unscored from today
unscored_today = conn.execute("SELECT COUNT(*) FROM jobs WHERE haiku_score IS NULL AND date(first_seen) = date('now', 'localtime')").fetchone()[0]
print(f'Unscored from today: {unscored_today}')

# No jd_full at all
no_jd = conn.execute("SELECT COUNT(*) FROM jobs WHERE jd_full IS NULL OR TRIM(jd_full) = ''").fetchone()[0]
print(f'No jd_full: {no_jd}')

# No jd_full from today
no_jd_today = conn.execute("SELECT COUNT(*) FROM jobs WHERE (jd_full IS NULL OR TRIM(jd_full) = '') AND date(first_seen) = date('now', 'localtime')").fetchone()[0]
print(f'No jd_full from today: {no_jd_today}')

# Stub jd_full (< 200 chars)
stub_jd = conn.execute("SELECT COUNT(*) FROM jobs WHERE jd_full IS NOT NULL AND LENGTH(TRIM(jd_full)) > 0 AND LENGTH(TRIM(jd_full)) < 200").fetchone()[0]
print(f'Stub jd_full (<200 chars): {stub_jd}')

# Enrichment tier distribution
print('\nEnrichment tier distribution (all):')
for row in conn.execute('SELECT enrichment_tier, COUNT(*) as cnt FROM jobs GROUP BY enrichment_tier ORDER BY cnt DESC'):
    print(f'  {row[0]}: {row[1]}')

# Enrichment tier for today
print('\nEnrichment tier distribution (today):')
for row in conn.execute("SELECT enrichment_tier, COUNT(*) as cnt FROM jobs WHERE date(first_seen) = date('now', 'localtime') GROUP BY enrichment_tier ORDER BY cnt DESC"):
    print(f'  {row[0]}: {row[1]}')

# Haiku score distribution
print('\nHaiku score distribution:')
for row in conn.execute("""SELECT CASE 
    WHEN haiku_score IS NULL THEN 'NULL' 
    WHEN haiku_score < 50 THEN '<50' 
    WHEN haiku_score < 65 THEN '50-64' 
    WHEN haiku_score < 80 THEN '65-79' 
    ELSE '80+' 
    END as band, COUNT(*) FROM jobs GROUP BY band ORDER BY band"""):
    print(f'  {row[0]}: {row[1]}')

# Sonnet score coverage  
has_sonnet = conn.execute('SELECT COUNT(*) FROM jobs WHERE sonnet_score IS NOT NULL').fetchone()[0]
print(f'\nJobs with sonnet_score: {has_sonnet}')

# Jobs with haiku but no sonnet that should have sonnet
missing_sonnet = conn.execute("SELECT COUNT(*) FROM jobs WHERE haiku_score >= 65 AND sonnet_score IS NULL AND jd_full IS NOT NULL AND LENGTH(TRIM(jd_full)) >= 200").fetchone()[0]
print(f'Jobs with haiku>=65, real jd_full, but no sonnet: {missing_sonnet}')

# Sample unscored jobs from today
print('\n--- Sample unscored jobs from today (first 10) ---')
for row in conn.execute("""SELECT dedup_key, title, company, enrichment_tier, 
    CASE WHEN jd_full IS NULL THEN 'NULL' WHEN LENGTH(TRIM(jd_full)) < 200 THEN 'STUB' ELSE 'OK' END as jd_status,
    LENGTH(description) as desc_len, LENGTH(jd_full) as jd_len,
    sources, pipeline_status
    FROM jobs WHERE haiku_score IS NULL AND date(first_seen) = date('now', 'localtime')
    LIMIT 10"""):
    print(f'  [{row["enrichment_tier"]}] [{row["jd_status"]}] {row["title"][:40]} @ {row["company"][:25]} | desc_len={row["desc_len"]} jd_len={row["jd_len"]} | sources={row["sources"]} | status={row["pipeline_status"]}')

# Sample unscored jobs from any time
print('\n--- Sample unscored jobs (any time, first 10) ---')
for row in conn.execute("""SELECT dedup_key, title, company, enrichment_tier, 
    CASE WHEN jd_full IS NULL THEN 'NULL' WHEN LENGTH(TRIM(jd_full)) < 200 THEN 'STUB' ELSE 'OK' END as jd_status,
    LENGTH(description) as desc_len, LENGTH(jd_full) as jd_len,
    sources, pipeline_status, first_seen
    FROM jobs WHERE haiku_score IS NULL
    ORDER BY first_seen DESC
    LIMIT 10"""):
    print(f'  [{row["enrichment_tier"]}] [{row["jd_status"]}] {row["title"][:40]} @ {row["company"][:25]} | desc_len={row["desc_len"]} jd_len={row["jd_len"]} | sources={row["sources"]} | status={row["pipeline_status"]} | seen={row["first_seen"]}')

# Check for jobs with description > 200 but no jd_full (eager promotion failure?)
eager_miss = conn.execute("SELECT COUNT(*) FROM jobs WHERE jd_full IS NULL AND description IS NOT NULL AND LENGTH(description) > 200").fetchone()[0]
print(f'\nEager promotion misses (desc>200 but no jd_full): {eager_miss}')

# Check for jobs with no description at all
no_desc = conn.execute("SELECT COUNT(*) FROM jobs WHERE description IS NULL OR TRIM(description) = ''").fetchone()[0]
print(f'No description at all: {no_desc}')

# Check recent runs
print('\n--- Recent pipeline runs ---')
try:
    for row in conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 5"):
        print(f'  {dict(row)}')
except:
    print('  (runs table not found or empty)')

# Check scoring costs
print('\n--- Recent scoring costs ---')
try:
    for row in conn.execute("SELECT model, COUNT(*) as cnt, SUM(cost_usd) as total_cost FROM scoring_costs GROUP BY model"):
        print(f'  {row[0]}: {row[1]} calls, ${row[2]:.4f}')
except:
    print('  (scoring_costs table not found or empty)')

# Budget status
print('\n--- Budget check ---')
try:
    month_cost = conn.execute("SELECT SUM(cost_usd) FROM scoring_costs WHERE strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')").fetchone()[0]
    print(f'  This month cost: ${month_cost or 0:.4f}')
except:
    print('  (cannot check budget)')

conn.close()

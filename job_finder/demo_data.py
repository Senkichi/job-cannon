"""Seed constants for demo mode (``job-cannon --demo``).

Every company name here is fictional — demo screenshots get shared widely and
must not imply that any real employer posted these listings. Slugs and URLs
point at plausible-but-nonexistent boards (clicking Apply in demo mode 404s,
which is acceptable).

Shape contract (consumed by :mod:`job_finder.demo_seed`):

- ``DEMO_COMPANIES``: rows for the ``companies`` table. The four ATS-``hit``
  entries make the Companies page demo well; the remaining feed companies
  deliberately have NO row so the suggested-companies surface (WP6) has
  material to work with.
- ``DEMO_JOBS``: 30 jobs. ``sub_scores`` present → the job is routed through
  ``persist_job_assessment`` (classification is DERIVED, never hand-set —
  the sextuples below were chosen to derive the intended class via
  ``derive_classification``). ``sub_scores=None`` → unscored (``jd_full`` is
  always non-empty so the job counts as scorable).
- ``source`` strings are the literal values the real ingest writes:
  ``linkedin`` / ``glassdoor`` (email parsers), ``portal_remoteok`` /
  ``portal_remotive`` / ``portal_himalayas`` (free portals), and capitalized
  ``Greenhouse`` / ``Lever`` / ``Ashby`` (ATS scanner ``company_source``).
- ``statuses``: pipeline chain applied IN ORDER via ``update_pipeline_status``
  so ``pipeline_events`` rows exist and the kanban + event feed populate.
- ``enrichment_tier='exhausted'`` + short ``jd_full`` → the scored row derives
  ``low_signal`` (demos the honest no-signal verdict).

Classification distribution: 6 apply, 9 consider, 5 reject, 2 low_signal,
8 unscored. Note: ``skip`` is intentionally absent — for integer 1-5
sub-scores that branch is unreachable (any value below 2 is a 1, which
rejects first).
"""

from __future__ import annotations

DEMO_COMPANIES: list[dict] = [
    {
        "name": "Northbeam Analytics",
        "ats_platform": "greenhouse",
        "ats_slug": "northbeamanalytics",
        "homepage_url": "https://www.northbeam-analytics.example",
        "careers_url": "https://boards.greenhouse.io/northbeamanalytics",
        "jobs_found_total": 9,
    },
    {
        "name": "Helio Systems",
        "ats_platform": "lever",
        "ats_slug": "heliosystems",
        "homepage_url": "https://www.heliosystems.example",
        "careers_url": "https://jobs.lever.co/heliosystems",
        "jobs_found_total": 6,
    },
    {
        "name": "Quanta Forge",
        "ats_platform": "ashby",
        "ats_slug": "quantaforge",
        "homepage_url": "https://www.quantaforge.example",
        "careers_url": "https://jobs.ashbyhq.com/quantaforge",
        "jobs_found_total": 4,
    },
    {
        "name": "Lumenalta Labs",
        "ats_platform": "greenhouse",
        "ats_slug": "lumenaltalabs",
        "homepage_url": "https://www.lumenaltalabs.example",
        "careers_url": "https://boards.greenhouse.io/lumenaltalabs",
        "jobs_found_total": 7,
    },
]


def _rationale(
    strengths: list[str], gaps: list[str], talking: list[str], skills: list[str]
) -> dict:
    return {
        "strengths": strengths,
        "gaps": gaps,
        "talking_points": talking,
        "resume_priority_skills": skills,
    }


def _jd(*paragraphs: str) -> str:
    return "\n\n".join(paragraphs)


DEMO_JOBS: list[dict] = [
    # ── apply (all sub-scores ≥3) ────────────────────────────────────────────
    {
        "company": "Northbeam Analytics",
        "title": "Senior Data Scientist",
        "location": "Remote (US)",
        "source": "Greenhouse",
        "source_url": "https://boards.greenhouse.io/northbeamanalytics/jobs/4821001",
        "source_id": "4821001",
        "salary_min": 170000,
        "salary_max": 225000,
        "days_ago": 9,
        "user_interest": "interested",
        "statuses": ["reviewing", "applied", "phone_screen", "technical"],
        "description": "Own experimentation and causal inference for the attribution platform.",
        "jd_full": _jd(
            "Northbeam Analytics builds marketing attribution tooling for mid-market "
            "e-commerce brands. You will own the experimentation platform end to end: "
            "designing geo-holdout and switchback tests, building the causal inference "
            "layer that turns raw spend data into incrementality estimates, and pairing "
            "with product engineers to ship models into the customer-facing dashboard.",
            "The stack is Python, dbt, and Snowflake, with model serving on a small "
            "internal FastAPI layer. The data science team is five people and reports "
            "directly to the CTO; you would be the senior-most IC and set the technical "
            "bar for review and methodology.",
            "We are remote-first across US time zones, offer meaningful equity, and run "
            "a documented, low-meeting culture. Salary range $170k-$225k plus equity.",
        ),
        "sub_scores": {
            "title_fit": 5,
            "location_fit": 5,
            "comp_fit": 4,
            "domain_match": 4,
            "seniority_match": 5,
            "skills_match": 4,
        },
        "rationale": _rationale(
            ["Exact title match at the right level", "Causal inference is a core strength"],
            ["No prior ad-tech attribution exposure"],
            ["Geo-holdout design experience", "Owning methodology as senior-most IC"],
            ["Causal inference", "Experimentation platforms", "dbt"],
        ),
    },
    {
        "company": "Helio Systems",
        "title": "Machine Learning Engineer",
        "location": "Remote (US)",
        "source": "Lever",
        "source_url": "https://jobs.lever.co/heliosystems/8f31c2",
        "source_id": "8f31c2",
        "salary_min": 165000,
        "salary_max": 215000,
        "days_ago": 6,
        "user_interest": "interested",
        "statuses": ["reviewing", "applied", "phone_screen"],
        "description": "Build the forecasting models behind grid-scale battery dispatch.",
        "jd_full": _jd(
            "Helio Systems operates software for grid-scale battery storage. Our ML team "
            "forecasts electricity prices and load at 5-minute resolution so dispatch "
            "decisions clear profitable arbitrage windows. You will own one of the "
            "forecasting model families end to end: feature pipelines, training, "
            "backtesting, and the on-call rotation for the models you ship.",
            "We care about engineering rigor more than novelty — most wins come from "
            "better features and tighter evaluation, not bigger models. Python, "
            "LightGBM, Ray, and Postgres; everything is in CI and reproducible from "
            "a single make target.",
            "Fully remote within the US. $165k-$215k depending on level, plus equity "
            "and a hardware budget.",
        ),
        "sub_scores": {
            "title_fit": 4,
            "location_fit": 5,
            "comp_fit": 4,
            "domain_match": 4,
            "seniority_match": 4,
            "skills_match": 5,
        },
        "rationale": _rationale(
            [
                "Forecasting and gradient-boosting background lines up exactly",
                "Strong eval culture",
            ],
            ["Energy-markets domain is new"],
            ["End-to-end model ownership", "Backtesting discipline"],
            ["Time-series forecasting", "LightGBM", "Python"],
        ),
    },
    {
        "company": "Quanta Forge",
        "title": "Senior ML Engineer, Ranking",
        "location": "Remote (US/Canada)",
        "source": "Ashby",
        "source_url": "https://jobs.ashbyhq.com/quantaforge/3d77a9",
        "source_id": "3d77a9",
        "salary_min": 180000,
        "salary_max": 240000,
        "days_ago": 4,
        "user_interest": "interested",
        "statuses": ["reviewing", "applied"],
        "description": "Own search ranking for a B2B parts marketplace.",
        "jd_full": _jd(
            "Quanta Forge runs a marketplace for industrial parts — 40M SKUs, brutal "
            "long-tail queries, and buyers who know exactly what they want if we can "
            "rank it. You will own the ranking stack: retrieval, the learning-to-rank "
            "layer, and the offline/online evaluation loop that keeps them honest.",
            "Current stack is a hybrid BM25 + embedding retrieval feeding a LambdaMART "
            "re-ranker; there is appetite to move the re-ranker to a cross-encoder where "
            "latency allows. You would be the second ML hire and shape that roadmap.",
            "Remote across US and Canada. $180k-$240k plus early-stage equity.",
        ),
        "sub_scores": {
            "title_fit": 4,
            "location_fit": 5,
            "comp_fit": 5,
            "domain_match": 4,
            "seniority_match": 4,
            "skills_match": 4,
        },
        "rationale": _rationale(
            ["Ranking/retrieval experience maps directly", "Comp at top of target range"],
            ["Marketplace domain is adjacent, not exact"],
            ["Offline/online eval loop design", "Second-hire roadmap ownership"],
            ["Learning-to-rank", "Embedding retrieval", "Evaluation design"],
        ),
    },
    {
        "company": "Lumenalta Labs",
        "title": "Staff Data Scientist",
        "location": "Remote (US)",
        "source": "Greenhouse",
        "source_url": "https://boards.greenhouse.io/lumenaltalabs/jobs/5530021",
        "source_id": "5530021",
        "salary_min": 195000,
        "salary_max": 260000,
        "days_ago": 12,
        "user_interest": "interested",
        "statuses": ["reviewing", "applied", "phone_screen", "technical", "offer"],
        "description": "Set the analytical agenda for a usage-based billing platform.",
        "jd_full": _jd(
            "Lumenalta Labs sells usage-based billing infrastructure to SaaS companies. "
            "As the first Staff-level data scientist you will set the analytical agenda: "
            "pricing elasticity models for customers, anomaly detection on metering "
            "streams, and the internal forecasting that finance actually trusts.",
            "This is a player-coach role — roughly 60% hands-on modeling, 40% raising "
            "the bar across a six-person analytics group through review and mentorship. "
            "You will work directly with the CEO on pricing research.",
            "Remote-first, US time zones. $195k-$260k, meaningful equity, 401k match.",
        ),
        "sub_scores": {
            "title_fit": 5,
            "location_fit": 5,
            "comp_fit": 5,
            "domain_match": 4,
            "seniority_match": 4,
            "skills_match": 4,
        },
        "rationale": _rationale(
            ["Staff scope with hands-on majority", "Top-of-band compensation"],
            ["Billing/fintech domain is new", "Player-coach split may reduce IC depth"],
            ["Pricing elasticity research", "Building trust with finance stakeholders"],
            ["Forecasting", "Anomaly detection", "Technical leadership"],
        ),
    },
    {
        "company": "Driftline",
        "title": "Senior Data Scientist, Growth",
        "location": "Remote (US)",
        "source": "linkedin",
        "source_url": "https://www.linkedin.com/jobs/view/9100442/",
        "source_id": "9100442",
        "salary_min": 160000,
        "salary_max": 210000,
        "days_ago": 2,
        "user_interest": "unreviewed",
        "statuses": ["reviewing"],
        "description": "Drive activation and retention modeling for a consumer fitness app.",
        "jd_full": _jd(
            "Driftline is a consumer fitness app with 2M MAU. The growth DS role owns "
            "the activation funnel: defining the metrics, building uplift models for "
            "lifecycle messaging, and running the experiment review that every product "
            "squad goes through before shipping.",
            "You will partner with two PMs and a four-person growth engineering pod. "
            "Stack: BigQuery, dbt, Python, and a homegrown experimentation service "
            "you would help mature.",
            "Remote in the US. $160k-$210k plus equity and a wellness stipend.",
        ),
        "sub_scores": {
            "title_fit": 5,
            "location_fit": 5,
            "comp_fit": 3,
            "domain_match": 4,
            "seniority_match": 4,
            "skills_match": 4,
        },
        "rationale": _rationale(
            ["Growth experimentation is a direct strength", "Clear senior scope"],
            ["Comp midpoint sits below target", "Consumer domain churn risk"],
            ["Uplift modeling wins", "Experiment review process design"],
            ["Experimentation", "Uplift modeling", "dbt"],
        ),
    },
    {
        "company": "Cobalt Harbor",
        "title": "Data Science Lead",
        "location": "Remote (Worldwide)",
        "source": "portal_remotive",
        "source_url": "https://remotive.com/remote-jobs/data/data-science-lead-1884302",
        "source_id": "1884302",
        "salary_min": 170000,
        "salary_max": 220000,
        "days_ago": 7,
        "user_interest": "unreviewed",
        "statuses": ["reviewing"],
        "description": "Lead a small DS team at a maritime logistics analytics company.",
        "jd_full": _jd(
            "Cobalt Harbor sells voyage-optimization analytics to shipping operators. "
            "The Data Science Lead runs a three-person team building ETA prediction, "
            "fuel-burn models, and port-congestion forecasts from AIS data.",
            "The role is 50% technical leadership, 50% hands-on. You will own the "
            "modeling roadmap, the hiring loop for the next two DS hires, and the "
            "quarterly accuracy review we publish to customers.",
            "Fully remote, async-friendly. $170k-$220k depending on location and level.",
        ),
        "sub_scores": {
            "title_fit": 4,
            "location_fit": 4,
            "comp_fit": 3,
            "domain_match": 4,
            "seniority_match": 5,
            "skills_match": 4,
        },
        "rationale": _rationale(
            ["Lead scope matches trajectory", "Geospatial time-series is a strength"],
            ["Maritime domain is unfamiliar", "Comp band wide and location-adjusted"],
            ["Published accuracy reviews", "Team-building experience"],
            ["Forecasting", "Geospatial modeling", "Team leadership"],
        ),
    },
    # ── consider (all ≥2, at least one 2) ────────────────────────────────────
    {
        "company": "Pinemont Health",
        "title": "Senior Data Scientist, Clinical Analytics",
        "location": "Boston, MA (Hybrid, 3 days)",
        "source": "linkedin",
        "source_url": "https://www.linkedin.com/jobs/view/9100871/",
        "source_id": "9100871",
        "salary_min": 150000,
        "salary_max": 190000,
        "days_ago": 11,
        "user_interest": "interested",
        "statuses": ["reviewing", "applied", "rejected"],
        "description": "Risk stratification and readmission modeling for a hospital network.",
        "jd_full": _jd(
            "Pinemont Health is the analytics arm of a six-hospital network in New "
            "England. The clinical analytics team builds readmission risk models, "
            "staffing forecasts, and quality-measure dashboards used by care teams "
            "daily.",
            "You will work with claims and EHR data under HIPAA constraints, partner "
            "with clinicians on model validation, and present quarterly to the chief "
            "medical officer. Hybrid: three days a week in the Boston office.",
            "Salary $150k-$190k with strong benefits and a pension contribution.",
        ),
        "sub_scores": {
            "title_fit": 4,
            "location_fit": 2,
            "comp_fit": 3,
            "domain_match": 2,
            "seniority_match": 4,
            "skills_match": 3,
        },
        "rationale": _rationale(
            ["Senior title and modeling scope fit"],
            ["Hybrid Boston requirement conflicts with remote target", "Healthcare domain is new"],
            ["Model validation with non-technical stakeholders"],
            ["Risk modeling", "Stakeholder communication"],
        ),
    },
    {
        "company": "Veldt Robotics",
        "title": "ML Engineer, Perception",
        "location": "Austin, TX (On-site)",
        "source": "linkedin",
        "source_url": "https://www.linkedin.com/jobs/view/9101203/",
        "source_id": "9101203",
        "salary_min": 160000,
        "salary_max": 200000,
        "days_ago": 8,
        "user_interest": "unreviewed",
        "statuses": [],
        "description": "Perception models for warehouse picking robots.",
        "jd_full": _jd(
            "Veldt Robotics builds autonomous picking arms for third-party logistics "
            "warehouses. The perception team owns object detection, pose estimation, "
            "and grasp-point selection running on edge GPUs at 30fps.",
            "You will improve detection robustness across novel packaging, build the "
            "active-learning loop that mines hard examples from the fleet, and care "
            "about latency budgets as much as mAP.",
            "On-site in Austin — the robots are here. $160k-$200k plus equity.",
        ),
        "sub_scores": {
            "title_fit": 3,
            "location_fit": 2,
            "comp_fit": 3,
            "domain_match": 2,
            "seniority_match": 4,
            "skills_match": 3,
        },
        "rationale": _rationale(
            ["Strong engineering culture, fleet-scale data"],
            ["Fully on-site in Austin", "Computer vision depth required"],
            ["Active-learning pipeline design"],
            ["Computer vision", "Edge inference"],
        ),
    },
    {
        "company": "Northbeam Analytics",
        "title": "Analytics Engineer",
        "location": "Remote (US)",
        "source": "Greenhouse",
        "source_url": "https://boards.greenhouse.io/northbeamanalytics/jobs/4821044",
        "source_id": "4821044",
        "salary_min": 130000,
        "salary_max": 165000,
        "days_ago": 5,
        "user_interest": "unreviewed",
        "statuses": [],
        "description": "Own the dbt layer powering customer-facing attribution metrics.",
        "jd_full": _jd(
            "Northbeam Analytics is hiring an analytics engineer to own the dbt "
            "project that powers every customer-facing metric. 600+ models, strict "
            "SLAs, and a real semantic-layer migration on the roadmap.",
            "You will pair with data scientists on metric definitions, enforce "
            "testing standards, and drive the Snowflake cost-optimization work that "
            "keeps margins healthy.",
            "Remote US. $130k-$165k plus equity.",
        ),
        "sub_scores": {
            "title_fit": 2,
            "location_fit": 5,
            "comp_fit": 3,
            "domain_match": 4,
            "seniority_match": 3,
            "skills_match": 3,
        },
        "rationale": _rationale(
            ["Company already in pipeline for a stronger-fit role"],
            ["Analytics-engineer title is a step sideways from DS/ML target"],
            ["dbt depth", "Semantic-layer migration"],
            ["dbt", "Snowflake"],
        ),
    },
    {
        "company": "Helio Systems",
        "title": "Data Platform Engineer",
        "location": "Remote (US)",
        "source": "Lever",
        "source_url": "https://jobs.lever.co/heliosystems/2b90e7",
        "source_id": "2b90e7",
        "salary_min": 150000,
        "salary_max": 195000,
        "days_ago": 10,
        "user_interest": "unreviewed",
        "statuses": [],
        "description": "Build the streaming ingestion layer for 5-minute market data.",
        "jd_full": _jd(
            "Helio Systems needs a data platform engineer to own streaming ingestion "
            "of market and telemetry data — Kafka, Flink, and a Postgres/Timescale "
            "serving layer feeding the forecasting team.",
            "Reliability is the product: missed market intervals are unrecoverable "
            "revenue. You will own SLOs, backfill tooling, and schema governance.",
            "Remote US. $150k-$195k plus equity.",
        ),
        "sub_scores": {
            "title_fit": 2,
            "location_fit": 5,
            "comp_fit": 3,
            "domain_match": 3,
            "seniority_match": 3,
            "skills_match": 4,
        },
        "rationale": _rationale(
            ["Platform skills transfer; company is a strong target"],
            ["Pure infra title, no modeling surface"],
            ["Streaming reliability story"],
            ["Kafka", "Data reliability engineering"],
        ),
    },
    {
        "company": "Driftline",
        "title": "Product Data Scientist",
        "location": "Remote (US)",
        "source": "glassdoor",
        "source_url": "https://www.glassdoor.com/job-listing/JV_KO0_8810224.htm",
        "source_id": "8810224",
        "salary_min": 140000,
        "salary_max": 180000,
        "days_ago": 13,
        "user_interest": "unreviewed",
        "statuses": [],
        "description": "Embedded DS for the social features squad.",
        "jd_full": _jd(
            "Driftline's social squad is hiring an embedded product data scientist. "
            "You will define engagement metrics for the new community features, run "
            "the A/B tests, and build the dashboards the squad lives in.",
            "Mid-to-senior level: you should be comfortable owning analysis end to "
            "end but will have a senior DS to lean on for methodology review.",
            "Remote US. $140k-$180k plus equity.",
        ),
        "sub_scores": {
            "title_fit": 3,
            "location_fit": 5,
            "comp_fit": 2,
            "domain_match": 3,
            "seniority_match": 2,
            "skills_match": 3,
        },
        "rationale": _rationale(
            ["Experimentation scope fits"],
            ["Level reads mid rather than senior", "Comp below target band"],
            ["Metric definition for 0-to-1 features"],
            ["A/B testing", "Product analytics"],
        ),
    },
    {
        "company": "Arc Light Media",
        "title": "Senior Analyst, Audience Insights",
        "location": "Remote (US)",
        "source": "portal_remoteok",
        "source_url": "https://remoteok.com/remote-jobs/318842",
        "source_id": "318842",
        "salary_min": 110000,
        "salary_max": 140000,
        "days_ago": 3,
        "user_interest": "unreviewed",
        "statuses": [],
        "description": "Audience segmentation and content performance analytics for a streaming studio.",
        "jd_full": _jd(
            "Arc Light Media produces documentary streaming content. The audience "
            "insights team turns viewing telemetry into segmentation, content "
            "performance reads, and greenlight recommendations.",
            "The role is SQL-heavy with some Python; modeling ambition is welcome "
            "but the day job is rigorous descriptive analytics presented to studio "
            "leadership.",
            "Remote US. $110k-$140k.",
        ),
        "sub_scores": {
            "title_fit": 2,
            "location_fit": 5,
            "comp_fit": 2,
            "domain_match": 3,
            "seniority_match": 2,
            "skills_match": 3,
        },
        "rationale": _rationale(
            ["Interesting media dataset"],
            [
                "Analyst title below target level",
                "Comp well below band",
                "Limited modeling surface",
            ],
            ["Greenlight decision support"],
            ["SQL", "Segmentation"],
        ),
    },
    {
        "company": "Ferrowood Logistics",
        "title": "Data Scientist, Forecasting",
        "location": "Chicago, IL (Hybrid, 2 days)",
        "source": "linkedin",
        "source_url": "https://www.linkedin.com/jobs/view/9101677/",
        "source_id": "9101677",
        "salary_min": 145000,
        "salary_max": 175000,
        "days_ago": 6,
        "user_interest": "unreviewed",
        "statuses": [],
        "description": "Freight volume and pricing forecasts for an intermodal logistics operator.",
        "jd_full": _jd(
            "Ferrowood Logistics moves intermodal freight across the Midwest. The "
            "forecasting team predicts lane volumes and spot pricing to drive "
            "capacity commitments worth eight figures annually.",
            "You will own two of the lane-level model families, modernize a legacy "
            "SAS pipeline into Python, and sit with the pricing desk weekly.",
            "Hybrid Chicago — two days a week on site. $145k-$175k plus bonus.",
        ),
        "sub_scores": {
            "title_fit": 3,
            "location_fit": 2,
            "comp_fit": 3,
            "domain_match": 3,
            "seniority_match": 3,
            "skills_match": 3,
        },
        "rationale": _rationale(
            ["Forecasting core is a direct fit", "Real decision impact"],
            ["Hybrid Chicago conflicts with remote target", "SAS legacy migration"],
            ["Pricing-desk partnership"],
            ["Time-series forecasting", "Python migration"],
        ),
    },
    {
        "company": "Tessellate Bio",
        "title": "Computational Scientist",
        "location": "Remote (US)",
        "source": "glassdoor",
        "source_url": "https://www.glassdoor.com/job-listing/JV_KO0_8811090.htm",
        "source_id": "8811090",
        "salary_min": 135000,
        "salary_max": 170000,
        "days_ago": 14,
        "user_interest": "unreviewed",
        "statuses": [],
        "description": "Analysis pipelines for high-throughput protein assay data.",
        "jd_full": _jd(
            "Tessellate Bio runs high-throughput protein stability assays for "
            "biopharma customers. Computational scientists own the analysis "
            "pipelines: signal processing, QC statistics, and the regression models "
            "that turn raw plates into deliverable reports.",
            "Wet-lab familiarity is not required but you will work daily with assay "
            "scientists. Python, pandas, and a growing Nextflow estate.",
            "Remote US. $135k-$170k.",
        ),
        "sub_scores": {
            "title_fit": 2,
            "location_fit": 5,
            "comp_fit": 3,
            "domain_match": 2,
            "seniority_match": 3,
            "skills_match": 3,
        },
        "rationale": _rationale(
            ["Statistical rigor transfers well"],
            ["Biology domain is far from background", "Title ambiguity on level"],
            ["Cross-functional work with assay scientists"],
            ["Statistical modeling", "Python"],
        ),
    },
    {
        "company": "Cobalt Harbor",
        "title": "Senior BI Developer",
        "location": "Remote (Worldwide)",
        "source": "portal_himalayas",
        "source_url": "https://himalayas.app/companies/cobalt-harbor/jobs/senior-bi-developer",
        "source_id": None,
        "salary_min": 120000,
        "salary_max": 150000,
        "days_ago": 9,
        "user_interest": "unreviewed",
        "statuses": [],
        "description": "Customer-facing reporting layer for voyage analytics.",
        "jd_full": _jd(
            "Cobalt Harbor needs a senior BI developer to own the customer-facing "
            "reporting layer — Metabase today, with an embedded-analytics rebuild "
            "planned for next year.",
            "You will translate voyage-analytics models into dashboards shipping "
            "operators actually open, and own the semantic definitions underneath.",
            "Fully remote. $120k-$150k depending on location.",
        ),
        "sub_scores": {
            "title_fit": 2,
            "location_fit": 4,
            "comp_fit": 2,
            "domain_match": 3,
            "seniority_match": 2,
            "skills_match": 2,
        },
        "rationale": _rationale(
            ["Company already on the radar"],
            ["BI title is off-target", "Comp below band"],
            ["Embedded analytics rebuild"],
            ["BI tooling", "Semantic modeling"],
        ),
    },
    # ── reject (at least one axis == 1) ──────────────────────────────────────
    {
        "company": "Brightlark Staffing",
        "title": "Data Scientist (W2 Only, URGENT)",
        "location": "Remote (US)",
        "source": "linkedin",
        "source_url": "https://www.linkedin.com/jobs/view/9102004/",
        "source_id": "9102004",
        "salary_min": None,
        "salary_max": None,
        "days_ago": 1,
        "user_interest": "unreviewed",
        "statuses": [],
        "description": "6-month contract data science role through a staffing agency.",
        "jd_full": _jd(
            "URGENT requirement for our direct client!! Data Scientist with Python, "
            "SQL, machine learning. 6-month contract, possible extension. W2 only, "
            "no C2C. Rate DOE.",
            "Must have: 5+ years Python, 5+ years SQL, 5+ years machine learning, "
            "5+ years cloud. Immediate interview slots available this week. Send "
            "resume and rate expectations.",
        ),
        "sub_scores": {
            "title_fit": 3,
            "location_fit": 4,
            "comp_fit": 1,
            "domain_match": 2,
            "seniority_match": 2,
            "skills_match": 2,
        },
        "rationale": _rationale(
            [],
            ["Undisclosed contract rate", "Staffing-agency repost with no end-client detail"],
            [],
            [],
        ),
    },
    {
        "company": "Veldt Robotics",
        "title": "Principal Research Scientist, Optimization",
        "location": "Austin, TX (On-site)",
        "source": "linkedin",
        "source_url": "https://www.linkedin.com/jobs/view/9102191/",
        "source_id": "9102191",
        "salary_min": 230000,
        "salary_max": 290000,
        "days_ago": 12,
        "user_interest": "unreviewed",
        "statuses": [],
        "description": "PhD-level research role in combinatorial optimization for robot fleets.",
        "jd_full": _jd(
            "Veldt Robotics seeks a Principal Research Scientist to lead our fleet "
            "optimization research: multi-robot task allocation, MILP and "
            "constraint-programming formulations, and publication-track work with "
            "our academic partners.",
            "Requirements: PhD in operations research or related field, first-author "
            "publications in combinatorial optimization, and prior principal-level "
            "research leadership.",
            "On-site in Austin. $230k-$290k plus equity.",
        ),
        "sub_scores": {
            "title_fit": 2,
            "location_fit": 2,
            "comp_fit": 4,
            "domain_match": 2,
            "seniority_match": 1,
            "skills_match": 2,
        },
        "rationale": _rationale(
            ["Compensation is excellent"],
            ["Hard PhD + publication-track requirement", "Principal research level mismatch"],
            [],
            [],
        ),
    },
    {
        "company": "Quanta Forge",
        "title": "Frontend Engineer",
        "location": "Remote (US/Canada)",
        "source": "Ashby",
        "source_url": "https://jobs.ashbyhq.com/quantaforge/5e12b0",
        "source_id": "5e12b0",
        "salary_min": 140000,
        "salary_max": 185000,
        "days_ago": 4,
        "user_interest": "unreviewed",
        "statuses": [],
        "description": "React/TypeScript engineer for the marketplace storefront.",
        "jd_full": _jd(
            "Quanta Forge is hiring a frontend engineer for the storefront team. "
            "React, TypeScript, and a design system in active development. You will "
            "own checkout-flow surfaces and pair closely with design.",
            "We ship daily behind feature flags and care about Core Web Vitals as a "
            "product metric.",
            "Remote US/Canada. $140k-$185k plus equity.",
        ),
        "sub_scores": {
            "title_fit": 1,
            "location_fit": 5,
            "comp_fit": 3,
            "domain_match": 2,
            "seniority_match": 3,
            "skills_match": 1,
        },
        "rationale": _rationale(
            [],
            ["Frontend discipline — no overlap with DS/ML target"],
            [],
            [],
        ),
    },
    {
        "company": "Orchard & Vine Commerce",
        "title": "Marketing Data Analyst",
        "location": "Remote (US)",
        "source": "portal_remoteok",
        "source_url": "https://remoteok.com/remote-jobs/319104",
        "source_id": "319104",
        "salary_min": 85000,
        "salary_max": 105000,
        "days_ago": 7,
        "user_interest": "unreviewed",
        "statuses": [],
        "description": "Campaign reporting and channel attribution for a DTC brand.",
        "jd_full": _jd(
            "Orchard & Vine Commerce is a DTC home-goods brand. The marketing data "
            "analyst owns campaign reporting across paid channels, builds the weekly "
            "performance deck, and maintains our Looker Studio dashboards.",
            "You should be strong in spreadsheets and SQL; Python is a plus but not required.",
            "Remote US. $85k-$105k.",
        ),
        "sub_scores": {
            "title_fit": 1,
            "location_fit": 5,
            "comp_fit": 1,
            "domain_match": 2,
            "seniority_match": 1,
            "skills_match": 2,
        },
        "rationale": _rationale(
            [],
            ["Junior analyst scope", "Compensation far below target"],
            [],
            [],
        ),
    },
    {
        "company": "Stonegate Financial",
        "title": "Quantitative Developer",
        "location": "New York, NY (On-site, 5 days)",
        "source": "linkedin",
        "source_url": "https://www.linkedin.com/jobs/view/9102440/",
        "source_id": "9102440",
        "salary_min": 200000,
        "salary_max": 275000,
        "days_ago": 10,
        "user_interest": "unreviewed",
        "statuses": [],
        "description": "C++ quant developer on a rates trading desk.",
        "jd_full": _jd(
            "Stonegate Financial's rates desk is hiring a quantitative developer. "
            "You will own pricing-library components in C++, optimize the intraday "
            "risk pipeline, and work shoulder to shoulder with traders.",
            "Requirements: expert modern C++, low-latency experience, and fixed "
            "income knowledge. Five days a week in our Manhattan office.",
            "$200k-$275k base plus substantial bonus.",
        ),
        "sub_scores": {
            "title_fit": 2,
            "location_fit": 1,
            "comp_fit": 5,
            "domain_match": 2,
            "seniority_match": 3,
            "skills_match": 1,
        },
        "rationale": _rationale(
            ["Outstanding compensation"],
            ["Five-day on-site NYC", "Expert C++ requirement does not match toolkit"],
            [],
            [],
        ),
    },
    # ── low_signal (enrichment exhausted + short JD) ─────────────────────────
    {
        "company": "Halcyon Grid",
        "title": "Data Scientist",
        "location": "Remote",
        "source": "portal_remoteok",
        "source_url": "https://remoteok.com/remote-jobs/319377",
        "source_id": "319377",
        "salary_min": None,
        "salary_max": None,
        "days_ago": 5,
        "user_interest": "unreviewed",
        "statuses": [],
        "enrichment_tier": "exhausted",
        "description": "Data scientist opening at an early-stage energy startup.",
        # Deliberately thin (>200-char I-13 floor, <1500-char low_signal
        # threshold) so the derived verdict is low_signal, not apply/consider.
        "jd_full": "Halcyon Grid is hiring a Data Scientist to join our growing team. "
        "You will work on energy data and help us make better decisions with machine "
        "learning. Python and SQL required; cloud experience a plus. We offer a "
        "competitive salary, equity, and a flexible remote culture. To learn more "
        "about the role and our mission, please apply on our website.",
        "sub_scores": {
            "title_fit": 3,
            "location_fit": 3,
            "comp_fit": 3,
            "domain_match": 3,
            "seniority_match": 3,
            "skills_match": 3,
        },
        "rationale": _rationale(
            [],
            ["Listing has almost no detail; enrichment could not recover a full description"],
            [],
            [],
        ),
    },
    {
        "company": "Mirefield",
        "title": "Machine Learning Engineer",
        "location": "Remote (Worldwide)",
        "source": "portal_himalayas",
        "source_url": "https://himalayas.app/companies/mirefield/jobs/machine-learning-engineer",
        "source_id": None,
        "salary_min": None,
        "salary_max": None,
        "days_ago": 8,
        "user_interest": "unreviewed",
        "statuses": [],
        "enrichment_tier": "agentic_exhausted",
        "description": "ML engineer posting with minimal public detail.",
        # Same deliberate thinness as the Halcyon Grid listing above.
        "jd_full": "Mirefield seeks a Machine Learning Engineer to join our fully "
        "distributed team. The ideal candidate has experience with model deployment "
        "and a passion for shipping. We are a stealth-stage company working on "
        "exciting problems at the intersection of AI and productivity. Further "
        "details about the technology stack and compensation will be shared during "
        "the screening call.",
        "sub_scores": {
            "title_fit": 3,
            "location_fit": 3,
            "comp_fit": 3,
            "domain_match": 3,
            "seniority_match": 3,
            "skills_match": 3,
        },
        "rationale": _rationale(
            [],
            ["Description too thin to assess; agentic enrichment found no public posting"],
            [],
            [],
        ),
    },
    # ── unscored (classification NULL, jd_full present → scorable) ───────────
    {
        "company": "Northbeam Analytics",
        "title": "Data Engineer, Pipelines",
        "location": "Remote (US)",
        "source": "Greenhouse",
        "source_url": "https://boards.greenhouse.io/northbeamanalytics/jobs/4821092",
        "source_id": "4821092",
        "salary_min": 145000,
        "salary_max": 185000,
        "days_ago": 1,
        "user_interest": "unreviewed",
        "statuses": [],
        "description": "Own ingestion pipelines pulling ad-platform data for attribution.",
        "jd_full": _jd(
            "Northbeam Analytics ingests spend and conversion data from a dozen ad "
            "platforms. This data engineer owns those pipelines: API quirks, "
            "backfill tooling, and the freshness SLAs our customers bet budgets on.",
            "Python, Airflow, and Snowflake. Remote US, $145k-$185k plus equity.",
        ),
        "sub_scores": None,
    },
    {
        "company": "Helio Systems",
        "title": "Senior MLOps Engineer",
        "location": "Remote (US)",
        "source": "Lever",
        "source_url": "https://jobs.lever.co/heliosystems/7a44c1",
        "source_id": "7a44c1",
        "salary_min": 165000,
        "salary_max": 205000,
        "days_ago": 0,
        "user_interest": "unreviewed",
        "statuses": [],
        "description": "Productionize the forecasting stack: serving, monitoring, retraining.",
        "jd_full": _jd(
            "Helio Systems is hiring a senior MLOps engineer to own model serving "
            "and the retraining/monitoring loop for our market forecasting stack. "
            "Ray Serve, MLflow, Grafana, and strong opinions about drift detection.",
            "Remote US. $165k-$205k plus equity.",
        ),
        "sub_scores": None,
    },
    {
        "company": "Lumenalta Labs",
        "title": "Applied Scientist, NLP",
        "location": "Remote (US)",
        "source": "Greenhouse",
        "source_url": "https://boards.greenhouse.io/lumenaltalabs/jobs/5530077",
        "source_id": "5530077",
        "salary_min": 175000,
        "salary_max": 230000,
        "days_ago": 2,
        "user_interest": "unreviewed",
        "statuses": [],
        "description": "Contract intelligence: extract billing terms from customer agreements.",
        "jd_full": _jd(
            "Lumenalta Labs is building contract intelligence — extracting billing "
            "terms from customer agreements so usage-based invoices configure "
            "themselves. You will own the extraction models, the eval suite, and "
            "the human-review loop.",
            "Remote US. $175k-$230k plus equity.",
        ),
        "sub_scores": None,
    },
    {
        "company": "Driftline",
        "title": "Experimentation Data Scientist",
        "location": "Remote (US)",
        "source": "linkedin",
        "source_url": "https://www.linkedin.com/jobs/view/9102815/",
        "source_id": "9102815",
        "salary_min": 155000,
        "salary_max": 195000,
        "days_ago": 1,
        "user_interest": "unreviewed",
        "statuses": [],
        "description": "Mature the experimentation platform: CUPED, sequential testing, guardrails.",
        "jd_full": _jd(
            "Driftline's experimentation platform needs a dedicated owner. You will "
            "bring variance reduction (CUPED), sequential testing, and automated "
            "guardrail analysis to a platform running 40 concurrent experiments.",
            "Remote US. $155k-$195k plus equity.",
        ),
        "sub_scores": None,
    },
    {
        "company": "Cobalt Harbor",
        "title": "Senior Data Scientist, Risk",
        "location": "Remote (Worldwide)",
        "source": "portal_remotive",
        "source_url": "https://remotive.com/remote-jobs/data/senior-data-scientist-risk-1884519",
        "source_id": "1884519",
        "salary_min": 160000,
        "salary_max": 200000,
        "days_ago": 3,
        "user_interest": "unreviewed",
        "statuses": [],
        "description": "Voyage risk scoring: weather, piracy corridors, port reliability.",
        "jd_full": _jd(
            "Cobalt Harbor is extending voyage analytics into risk: scoring routes "
            "on weather exposure, congestion risk, and port reliability. You will "
            "build the scoring models and the backtesting that proves them.",
            "Fully remote. $160k-$200k depending on location.",
        ),
        "sub_scores": None,
    },
    {
        "company": "Tessellate Bio",
        "title": "Senior Bioinformatics Engineer",
        "location": "Remote (US)",
        "source": "linkedin",
        "source_url": "https://www.linkedin.com/jobs/view/9103002/",
        "source_id": "9103002",
        "salary_min": 150000,
        "salary_max": 190000,
        "days_ago": 4,
        "user_interest": "unreviewed",
        "statuses": [],
        "description": "Scale the Nextflow estate processing assay sequencing data.",
        "jd_full": _jd(
            "Tessellate Bio's sequencing volumes doubled twice last year. This role "
            "owns the Nextflow pipeline estate: performance, cloud cost, and the "
            "shared module library that other scientists build their analyses on.",
            "Remote US. $150k-$190k plus benefits.",
        ),
        "sub_scores": None,
    },
    {
        "company": "Arc Light Media",
        "title": "Data Scientist, Recommendations",
        "location": "Remote (US)",
        "source": "portal_remotive",
        "source_url": "https://remotive.com/remote-jobs/data/data-scientist-recommendations-1884602",
        "source_id": "1884602",
        "salary_min": 145000,
        "salary_max": 185000,
        "days_ago": 0,
        "user_interest": "unreviewed",
        "statuses": [],
        "description": "First recommendations hire for a documentary streaming catalog.",
        "jd_full": _jd(
            "Arc Light Media wants its first recommendations hire. Cold-start is "
            "the whole problem: a small catalog of documentaries, rich editorial "
            "metadata, and viewers who churn if the first row misses.",
            "Remote US. $145k-$185k.",
        ),
        "sub_scores": None,
    },
    {
        "company": "Ferrowood Logistics",
        "title": "Senior ML Engineer, Routing",
        "location": "Remote (US)",
        "source": "portal_remoteok",
        "source_url": "https://remoteok.com/remote-jobs/319561",
        "source_id": "319561",
        "salary_min": 160000,
        "salary_max": 200000,
        "days_ago": 2,
        "user_interest": "unreviewed",
        "statuses": [],
        "description": "ML-assisted load matching and routing for intermodal freight.",
        "jd_full": _jd(
            "Ferrowood Logistics is building ML-assisted load matching: pairing "
            "freight with capacity under time-window and equipment constraints. "
            "You will own the matching models and their integration with the "
            "optimization layer.",
            "Remote US. $160k-$200k plus bonus.",
        ),
        "sub_scores": None,
    },
]

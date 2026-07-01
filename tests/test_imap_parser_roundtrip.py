"""Tests for IMAP parser round-trip with real .eml fixtures.

Tests that every sender parser works when fed IMAP-fetched RFC 5322 .eml messages.
"""

import email
import email.policy
from pathlib import Path

import pytest

from job_finder.sources.email_senders import SENDER_PARSERS
from job_finder.sources.imap_source import ImapSource

# Golden expected values for one representative fixture per sender.
# Canonical contract: (title: str, company: str, source_url: str, posted_date: str | None)
# posted_date is the parser's full naive-UTC datetime as ISO 8601 (e.g.
# "2026-05-16T23:38:54"), or None when the parser yields no date. The full timestamp
# (not just the date) pins _extract_date so same-day tz/clock regressions are caught;
# it is deterministic because it derives from the fixed .eml Date header.
# source_url falls back to url only if source_url is empty (mirrors existing test logic).
GOLDEN_EXPECTED = {
    "jobalerts-noreply@linkedin.com": {
        "fixture": "linkedin_alert.eml",
        "jobs": [
            (
                "Data Scientist, Business",
                "OpenAI",
                "https://www.linkedin.com/jobs/view/4376221593/",
                None,
            ),
            (
                "Data Scientist 5 - Member Experience for Games",
                "Netflix",
                "https://www.linkedin.com/jobs/view/4359584630/",
                None,
            ),
            (
                "Data Scientist 4/5 - Identity DSE",
                "Netflix",
                "https://www.linkedin.com/jobs/view/4348419825/",
                None,
            ),
            (
                "Senior Data Scientist",
                "Abridge",
                "https://www.linkedin.com/jobs/view/4318554538/",
                None,
            ),
            (
                "Data Scientist - POC",
                "Sardine",
                "https://www.linkedin.com/jobs/view/4296271224/",
                None,
            ),
            (
                "Senior Data Scientist - Alt Defense",
                "Roblox",
                "https://www.linkedin.com/jobs/view/4385760798/",
                None,
            ),
            (
                "Marketing Analytics Manager",
                "Solace",
                "https://www.linkedin.com/jobs/view/4394747513/",
                None,
            ),
            (
                "Sr Growth Analytics Manager",
                "LendingClub",
                "https://www.linkedin.com/jobs/view/4364023333/",
                None,
            ),
            (
                "Data Scientist, Platform and B2B Products",
                "OpenAI",
                "https://www.linkedin.com/jobs/view/4306142538/",
                None,
            ),
            ("Data Scientist", "Tarro", "https://www.linkedin.com/jobs/view/4298469184/", None),
        ],
    },
    "jobs-noreply@linkedin.com": {
        "fixture": "linkedin_jobs.eml",
        "jobs": [
            (
                "Data Science Manager, Shopping Experience",
                "Instacart",
                "https://www.linkedin.com/jobs/view/4394937667/",
                None,
            ),
            (
                "Staff Data Scientist - Product",
                "Ladders",
                "https://www.linkedin.com/jobs/view/4396656802/",
                None,
            ),
            (
                "Insights & Strategy Lead",
                "Propel, Inc",
                "https://www.linkedin.com/jobs/view/4393035003/",
                None,
            ),
            (
                "Applied Data Scientist",
                "Ladders",
                "https://www.linkedin.com/jobs/view/4396658757/",
                None,
            ),
            (
                "Head of Data & Analytics",
                "Vanta",
                "https://www.linkedin.com/jobs/view/4392489136/",
                None,
            ),
            (
                "Staff Analytics Engineer",
                "ClassDojo",
                "https://www.linkedin.com/jobs/view/4394366375/",
                None,
            ),
            (
                "Director, Product Analytics",
                "Ladders",
                "https://www.linkedin.com/jobs/view/4396663712/",
                None,
            ),
            (
                "Head of Data",
                "BetterSleep\u2122",
                "https://www.linkedin.com/jobs/view/4390226320/",
                None,
            ),
            (
                "Senior Manager, Growth Optimization",
                "Lime",
                "https://www.linkedin.com/jobs/view/4388214178/",
                None,
            ),
        ],
    },
    "noreply@glassdoor.com": {
        "fixture": "glassdoor_2.eml",
        "jobs": [
            (
                "Sr Data Engineer",
                "Disney Entertainment and ESPN Product & Technology",
                "https://www.glassdoor.com/job-listing/j?jl=1010135954642",
                "2026-05-16T23:38:54",
            ),
            (
                "Senior Data Engineer",
                "Verkada",
                "https://www.glassdoor.com/job-listing/j?jl=1010136663267",
                "2026-05-16T23:38:54",
            ),
            (
                "Data Engineer, Product Data Warehouse, Go-To-Market",
                "Google",
                "https://www.glassdoor.com/job-listing/j?jl=1010136020292",
                "2026-05-16T23:38:54",
            ),
            (
                "Sr. Data Engineer",
                "Visa Inc.",
                "https://www.glassdoor.com/job-listing/j?jl=1010136532359",
                "2026-05-16T23:38:54",
            ),
            (
                "Senior Data Engineer - Data Lead",
                "Foundry Robotics Inc.",
                "https://www.glassdoor.com/job-listing/j?jl=1010136720005",
                "2026-05-16T23:38:54",
            ),
            (
                "Senior Software Engineer - AI Data Applications",
                "Motional",
                "https://www.glassdoor.com/job-listing/j?jl=1010136132155",
                "2026-05-16T23:38:54",
            ),
            (
                "Data Center Controls Engineer, Cyber Security",
                "Google",
                "https://www.glassdoor.com/job-listing/j?jl=1010136020391",
                "2026-05-16T23:38:54",
            ),
            (
                "Senior Data Engineer",
                "Adobe",
                "https://www.glassdoor.com/job-listing/j?jl=1010135479306",
                "2026-05-16T23:38:54",
            ),
            (
                "Machine Learning Data Engineer",
                "Apple",
                "https://www.glassdoor.com/job-listing/j?jl=1010136870996",
                "2026-05-16T23:38:54",
            ),
            (
                "Software Engineer, AI/ML Data and Training Infrastructure",
                "Google",
                "https://www.glassdoor.com/job-listing/j?jl=1010129861229",
                "2026-05-16T23:38:54",
            ),
            (
                "Data Integration Engineer",
                "Tata Consultancy Services",
                "https://www.glassdoor.com/job-listing/j?jl=1010136367584",
                "2026-05-16T23:38:54",
            ),
            (
                "Data Center Engineer III",
                "Google",
                "https://www.glassdoor.com/job-listing/j?jl=1010136020325",
                "2026-05-16T23:38:54",
            ),
        ],
    },
    "alert@indeed.com": {
        "fixture": "indeed_alert.eml",
        "jobs": [
            (
                "Evaluation Team Lead, Business Intelligence, Google.org",
                "Google",
                "https://www.indeed.com/rc/clk/dl?jk=49cda075c73ac1ee&from=ja&qd=RnZhMybXSk4M3QtTVGXWofcrJs4bdU7tKjWaLzU9Y6ZRLfe37moralhGskTRUAyTJv5UBF7Zamhw6MFXlcIuc2Yuj4OUnC9hEeKRUyddr76lpPeq_lbj1w2styiL0i35&rd=EvLisMDFuvoFoCasnNPvI32LhTYP0op_TBenMVhVTPM&tk=REDACTED&alid=69b38a1b24b03c0df228806b&bb=yCTXdVq95KR6IQNgN15ogycewlcI-SxEFAUUmXZ1b5B4Xro_-phfX1VMPnS9N2xPiNq17deoNdU78WyEC56l6WD95RLN8ms0wr5muIcZ0lx_2Iz1nULnerg1m7R8zYqHKLwgCr-mDORybJ_YFJEdJw%3D%3D&g1tAS=true",
                "2026-03-19T09:35:11",
            ),
        ],
    },
    "donotreply@match.indeed.com": {
        "fixture": "indeed_match.eml",
        "jobs": [
            (
                "Agentic Workflow Lead",
                "Smart-Tek",
                "https://www.indeed.com/pagead/clk/dl?from=jobi2a_multijob-en-US_email&jrtk=REDACTED&mo=r&ad=-6NYlbfkN0DP7N_JgDagYY8-Mk0WwzF0Q0gIEsWRfzc2JbQn8QKLxA6HyinvnSHjVbuaE5I-U2a4S_W4aKg-cGU6Ik2Ju7A35EONxpcfaSq9YP4K4OP5qYr4EW2gdNG1yvdhefgIWRWwjhht0snAnqiD6Gnm9l2JjnLMgi_YqlBstrdk25yY0UaNFWXeBKscunWJLx6m6M7DKSVAbq0hvvv0tAsyB4jsnfm1K9M0vZ4Scatht6JJbdud327ZYC3R5v_zUYV656CtvuJI6GPjZqLuP917Ilwn98ck3yAZRy-KnaXU6lqcVOM4W7S8FUfXtmtiPD5UzbWPnqL4WAIX2GVrz8G0K8j4A_WsJkOqhQ7zYUC8_n4j5hBalkGImIZUPUpRtsRvkWsJQ3guoYzP-7SXriPbTCqdKrcgfI01WVpbZTHFit5isQB27jjQo-QlFZnsZPh25dIpdqkp38Tl0fCepkmds-xqbM5dZcIn-baT2jEqa99c71UUV1TGwfb7ypEDzjBdhyMqwdBuak2fsTvrChfH1AYE4LUH4K-Kdou8gZNhxa3K70SGJ_BcJpSNDUpavAKf83TdXCpk7wgRcEnrvK_sCYB40ZCh9wwOII53dgQTYZikcucj48PSIH8teXpHIEnY2Q923n31K1jDqI8SS6HuSbYyg14yacCajVHeJFLx-x2akF1F4Ey2CKivoNarJTGv2EboVlR1ZeV7oizoMkVv19E-xVcfhSHCRA1uM4dKFsVA-YbW9iXy1VRv&rm=2&xkcb=SoB86_M3jNcH4LgIRz0DbzkdCdPP&jsa=1373&camk=UoKtGZLa3XIK3G_-FSLJ2A%3D%3D",
                "2026-05-12T05:20:22",
            ),
            (
                "Senior Analytics Engineer",
                "DataTrek",
                "https://www.indeed.com/pagead/clk/dl?from=jobi2a_multijob-en-US_email&jrtk=REDACTED&mo=r&ad=-6NYlbfkN0AetoHIirK34P7s2vqFuRIt-YpcTdvxXDAKddCyZPRslFnPTWrwnUK85TIuyqod6Yim2rDn4Ahb2JoxCKASJikbMmV8ZMTpXCJluxq8hnd-dupNeYBNhkGK0vMteOImLO-tshgZEvNi0PV3m2tHfUiO60PvVqIz3PR8uO9vbRC67c-IZMVw4-fPlFJpjKr2miY04wrKKArIbt-el-8AMLFWDDhXK1wHn_wwu5EUDL1Gb8Tv6XFeENhdpYFCjRt5xVCld-DDswzhoCulKu2mllQM4Fwtnd_mqpr5wXRGey-_UlPDkq2xjFT41Ktr0eIqkoKTzwxas9kB8xyRd8hn8mLzqCuIXQrK8_JIjT-5lBSV__RsjwE9KHBsPMBj4uO5u2CYfm_zYZQevTD9xPl9m9lOAQwlYtsToq8u2jNiicK2d0OS_26SLijwCKMr4iT5QTDZ-OZRXd9j1-OF374cppo6_fh66OA-Y19h2rZ1xnz4I2P04qNhqaNOip1IURZ5N5DVzioxoPp-JdZugca8dI0h93TKDoz4_esQgPxDC4OAAyVQftockJJcogoiLm6B3dLc4YVU7tXbPz9CuBmdmqMdTQ91AUo_6pMClHuQ05Zauc1MGCDXxyC0xPMslYawaJAM95WI5NkykgCh8cCH43PhYc2PqXQpF3IUAstO0ZMNhF1jFsJf506BgRtbuO2y5E7U3_sI81uYKNNiXk_GQbzEMW0YKyDzf4zPHFJXIcHyejIrtHeOZ80D&rm=2&xkcb=SoAy6_M3jNcH4KAIRz0ObzkdCdPP&jsa=1374&camk=C3EPSzFlQw_egpP6ekN0og%3D%3D",
                "2026-05-12T05:20:22",
            ),
            (
                "Data Analyst",
                "BCforward",
                "https://www.indeed.com/pagead/clk/dl?from=jobi2a_multijob-en-US_email&jrtk=REDACTED&mo=r&ad=-6NYlbfkN0D-C5SPsE_WbmqnViNawo540GcwQzW7rJEqTEx1Y8crPOisBwWDYfS9xr5tAAuLzmP3I6Ai7p-TrA1D8sa3Bk7p21mvduSQqdT2Bm41lsJcNwXpq_fpP6erkBvzz4uRRmMc-OxqHr2VQkxaqZWhsdgkP7POEuphuyABPIqom8i7sh1Q95BOCefCAFLAUg58woy-RakcqBBx69PQxe1mv-MxjWHRCW-3aG7IxjBqcsXYNJHlJUpIswT2NE_qI5y5jzmWkGPkbLeG0fvzXg_28TQzotofPCNA3a2wqxZL7XWN5YV90odt9hf20Gtwm1hdvwnjKIvG2FvSaEUcCnllo6TGnSapnYJrh8djTVC_dHOAari0b4vO1ZDmLANa_Hu3HQlqH-bK3rwkCThfLNbwv9tQNM_fWPpdSEgetq6adtbNqxgnGj1WiosQsTjMFtuh8ByO2YvpsayrXBScpnFsI4Kkzn2qeW-GKsW_FQfax_b8h05YH7-AmQ1D_Z9mPkd1kfo9Ryzwin0H7wG4z_srDYgnqcs_P3WbgY11jct10E1vYLQuEdgIBBjFGO6UFtAxpa66mI-neb91Km6yCpmNrXNrWTBpAXlbX7_J8AhQtzzXmRzVRqJhHs-gfsOnPIOd968rCmRpnxTTFAGvXozW6XTsL2jG5YbSn8b04FPD1OpKUNFy2eaTbRi1GqQd0PpiRKuZEqY9hmdZgHRoxK9npxjbiwsgIovBWiU%3D&rm=2&xkcb=SoBV6_M3jNcH4KAIRz0GbzkdCdPP&jsa=1374&camk=C3EPSzFlQw_-eWOlhT5tbw%3D%3D",
                "2026-05-12T05:20:22",
            ),
            (
                "Senior Data Architect",
                "TCA Consulting Group Inc.",
                "https://www.indeed.com/pagead/clk/dl?from=jobi2a_multijob-en-US_email&jrtk=REDACTED&mo=r&ad=-6NYlbfkN0Aobqe4mQdrTym_OyFCqzynb13PIBNU1hL0lnlL5gTMpCyWr9H8P7-6wxkwegz1O05JxeBg4RB5_KAueFMK2gzT7iuo_MQ3LycJxJ5cA4d4BqM0Z20zdwVO9QdwQK04alnQWuXqHaByY9zTScjRW6iRY5jPs0T4nv9Kp2-jYi4bZZdh_wyilznsCIzUWIDH7S8ze76lhdqB4uKMSJIAR8Yh3LUXhWsVvPQK_eQfZFGuPOvlAlRzlQP3Zk6Vzw_4ZGwDEogVjkaz89fNVqQCyyI3jv7U15I1qOMfbFAjLwqnOc-ktacQp4krpP7UoDXwkl9wnX8G8IbupARoCb_jfnikzDz-rLbNb6WgaBgdICbQGCu16SgqocbqLCybwBgWR0CTl1wjD-DvaE8-PDrdn0dKzQjEQj-LyYRngS-Nz5z8zLcwLL-epAk56LBjBEJBtYZcos_DcZDAO4-AYJwDHdHBWh_D79NcFcHje2-G6gjVLMOzVuyF6CuQZq0N-G2TQYSKyU2zzFq-dZdJFdgCSJMoK26c9tNcqoXcPm98Zbce9YCfkFwTQP6vLqGxRkDcBu0NDmjye6_rb7ceFaJXZ2IUH6qLZfxDMSkpWl9Shehx5NKhJAFR_1jxHmDARhx0h8CY_ht3xTGnhr0xQJuMF--oQ7lGU-SdU_6vL6Hn7GvJa0RCAXUsu1hSF-I7tP7UvMr2Yzb6gbPI0FRKAhIIaK8DMHqENY_fXSl4PVPEm6X6YZmFWXcvJOab&rm=2&xkcb=SoDG6_M3jNcH4KAIRz0YbzkdCdPP&jsa=1374&camk=4HOcmqOLYrADBTkIfqkFGw%3D%3D",
                "2026-05-12T05:20:22",
            ),
            (
                "Data Architect / Lead Data Strategist (Remote)",
                "GSK Solutions Inc",
                "https://www.indeed.com/pagead/clk/dl?from=jobi2a_multijob-en-US_email&jrtk=REDACTED&mo=r&ad=-6NYlbfkN0BKL-Z7gatACP1Cz6bVrcfudhea9O5J3kB0jN_foYnGjGGXJ-Es83HyWPh3dWRCEQVgKPatTEROv8qC-xVqc2f-Kapq_4JjpUDlLEsZklcE-7DJXiZuzGgYyxmtyNwN5x8sF5UMp4cP_SJX1zg77_BT9PWiDyk8X_ZuOWkoRBkejIP_P-UMBk4raiC-AlFgeY8B9HwyXnwfhZIdBbx6g7MBAbi4NgdCsZ4ddkN4Mz-MW8C8G6ZDg24GJlEnF0bmadH2hux6RdoGjkory-rizQPap8mbddZsK72xxt9iNuuC8-3IaKAz-7SAJFURI3LJGLkDDkx5tRQYiDTcygJRZRdl2MceZj82hqS5tOX0FuCDVDdEeu0T3ipA4nbchv4yAldiY1VTUEnB7AuMtcj5egdXaTDNu4jurjluObHACXcC3ddzGoERVP_1pMlZnYD23s8rA3jadBockAplOCxV491uiMVieYq39yNEzBemCw679B1VV9prxIbgnq4lv9xPjsIh7Qg1sSe13rO8vs_vuENj7C8wF0xk6x0A15Do6p3zkhwo2Q9c5K4PaPW9DYsJDniuHjWmDpsjGS0avUW33dMdeO1NieoNLRfTfjpCeOgTh-EniMHsu1HUpDEDoOE5MioeV84SImCVHuq7HcE_cRnqayorZbOOQW-ae_y5hH3_AhDrbqJvjf-A8yrDAnbV8nu17_jgAx8AZVx5o0mj6TToMFzi4U82u9aM_Rt72lVyJspZg3cEJ304FwME2xKLh5I-H1pIdBGRcw%3D%3D&rm=2&xkcb=SoCI6_M3jNcH4KAIRz0SbzkdCdPP&jsa=1374&camk=UoKtGZLa3XJsHrj9fF4Kcw%3D%3D",
                "2026-05-12T05:20:22",
            ),
        ],
    },
    "no-reply@us.greenhouse-jobs.com": {
        "fixture": "greenhouse.eml",
        "jobs": [
            (
                "Lead Data Analyst",
                "ParetoHealth",
                "https://job-boards.greenhouse.io/paretocaptiveservicesllc/jobs/4647776006",
                None,
            ),
        ],
    },
    "hello@trueup.io": {
        "fixture": "trueup.eml",
        "jobs": [
            (
                "Senior Financial Analyst, Google One AI\xa0Subscriptions",
                "Google",
                "https://www.google.com/about/careers/applications/jobs/results/90752443096146630-senior-financial-analyst-google-one-ai-subscriptions",
                "2026-04-29T23:30:24",
            ),
            (
                "Director, AI Governance, Automation &\xa0Analytics",
                "AlphaSense",
                "https://job-boards.greenhouse.io/alphasense/jobs/8292394002",
                "2026-04-29T23:30:24",
            ),
            (
                "Business Development Lead, Data for\xa0Gemini",
                "Google",
                "https://www.google.com/about/careers/applications/jobs/results/86359363714720454-business-development-lead-data-for-gemini",
                "2026-04-29T23:30:24",
            ),
            (
                "Cloud Senior Financial Analyst, Gemini Enterprise and\xa0Agents",
                "Google",
                "https://www.google.com/about/careers/applications/jobs/results/135863914830144198-cloud-senior-financial-analyst-gemini-enterprise-and-agents",
                "2026-04-29T23:30:24",
            ),
            (
                "Director of Growth Analytics,\xa0Merchant",
                "Sunbit",
                "https://sunbit.com/careers/?job=4C.86C",
                "2026-04-29T23:30:24",
            ),
            (
                "Staff Data\xa0Analyst",
                "Okta",
                "https://www.okta.com/company/careers/opportunity/7792010?gh_jid=7792010",
                "2026-04-29T23:30:24",
            ),
            (
                "Principal Product Manager, Data and AI\xa0Platform",
                "Blackbaud",
                "https://blackbaud.wd1.myworkdayjobs.com/en-US/ExternalCareers/job/Remote---Anywhere---USA/Product-Manager--Data-and-AI-Platform_R0013844",
                "2026-04-29T23:30:24",
            ),
            (
                "Area Project Controls Lead, Leased Data\xa0Centers",
                "Meta",
                "https://www.metacareers.com/jobs/945080131354140",
                "2026-04-29T23:30:24",
            ),
        ],
    },
    "monster@notifications.monster.com": {
        "fixture": "monster.eml",
        "jobs": [
            (
                "Technical Product Manager, Data",
                "Credit Acceptance Corporation",
                "http://click.monster.com/f/a/Uu3UcF3uWRcXS8ub0AphZw~~/AAAAARA~/I8BRbXNer-54qtE6_h9scAFhoYiSa_IpcmRniKaB4KrjoT0ClnoYQySFu6dN02UdjUgxNpfKyXq_MV38afaYz9N273oQsnQ2QI_ggo8_VEaR4qSWO_dnmOkLtMI3F-nyIulxGlWwEnEemtOmv5mgON8UPjERLoHdTElQwyEjvsK2T5z0i-xBr2QFmJ-tcocBA2W8XCS17jq5sThk_zWI8PTPptlQHgm1G6BCJlDlYHvii4hw_jKhi3SgFj074lJkdpTPTINouFocQ7rzuJ9qJ9KZ18va1IsWkjAM_KW6G0Qm4aYchyTuDNy4y9QkTgXqlYmoOn5-r_cbo2bv_HYO2vcR2SUOIXgYT7UIMbN5wxVZ0FH_4jXM_d4u8zyjOqAWFpZ1FpGu-QkfdZcA7KjF4VNYHf9v7K5UDqSZtFXKC9kSljXPDzVdYHcnqcmWqvAGtzGX_35P3TrrNy1EdHZWJBt2iuCIDD40obMscOdc8xqSL6c2yOsjsXXm_koW6FhewZfZo_tyEI98SvY05gohZViuWF27HgzT9JLdnHzESeRt2c53c_8dIO-lTSgAfKllT4EDtDSGrIvx2qUbUWFeijaqDOjmZDpxIn7Y5hfI6fFuWU7m7BBx2rpp9zOUzruhPAMcLQFyNy8lMRyiZK3hviJL8yJOV2uxgJhEIB98Glw~",
                "2026-05-15T09:37:43",
            ),
            (
                "Case Manager - Electronic Discovery",
                "Jobot",
                "http://click.monster.com/f/a/NDvbW3ZjDOQbofRDyVo3ZQ~~/AAAAARA~/_EWeoPiYkJw5SdhTSM_5oiO-lYJNKdiN89BNX58yAgEx0d9rs3IrZFwPvLos_2XilxIL7UaIWr2YPABmNCqhQCw3U_GRdeTYRsiQrPIDPFT2nWN4YOTFPWGASja8bQfbiAjISYm8XhZYqrVjU8o1gnyyceSanOqsmi0YUPzZTFmShcqzwRTnAhtpNOV-vNDwwdaPwoSb2DUv-sh5BQonBtd-XgM5FzzEpARglamio8pbQiDPnF6iolxR5nOo5uVsvnu6czZLRLwC2Ub3a7L54Y8qlzVoplzRGhDVYWU41FUShvOnd0IAqycb-Y-XvwU9veJ01SoH5zvkBLdqalRKV7e1xuRXjiGk7OiEFjdY9_tWQcLBx5jt_GsYkMtQ3_jvapTowSl0_avdTc05hlCYkoHqfs8rUiBE9ZA18IsFagp4LvsDvh7-kwCGwrhWqjJZog7CD7lLugTJOOUacfUHn8OohsaarJ1yuZ1tMAhT_AgvwFbMRaIAQom7fn2BWGMu9I3cZb9yicVBYDdxFr7vIVFGKVYVcak43CdZo88QB4rCZxwdXufuXjdT-MrRBbMzVoDVjU1hU1sw6cc1fyTLmhDY2n4EobaDnXRD37JbY0HRMcvFoXSxYi8yzV1Lnk5Dd3Bflnj2JKx_kaFO0rNJXld8w7wDnEe5vNEpVYX9ybI-6cQLjQQlULAseDBnEE7d2K1qysQKmdmmG8llw2KtqQ~~",
                "2026-05-15T09:37:43",
            ),
            (
                "Engagement Manager",
                "Censeo Consulting Group",
                "http://click.monster.com/f/a/unJ_iH_DXRg2mplJfLsRsA~~/AAAAARA~/CEGzFp213PIvzXOt7em07ycCa47LvtD_567TIOkuDrh5-CZeNbPIxwXUAGA4t5J6vBY32fXnM1pEbs927pLYe8sOyj6obpkrIwnF8bYq6IZ08BRFIc6rKzrV8wQ4ImF0Wrl8I2HskuuH5kNwLMXcZ73su06TYjXKPsKUVNemn9kfJlzRrwnQJRfYzLxTsQOiF-aVGR5uFgmKFyHBs8gDU5XxvcLL8Xc6rYYjpfnxJwCLW88drCmbp247oAZwiMuYYV02X6Hshd2ak07mLcmWykUlYqo2Rc060g_3SzTm7VI6ZNKOyZwwljNhuneF1C31ODPM8_b7-R7byUUxj3hAhwm1ZabmisQnNXUptLzt6MBiltVRlsJkivG0GQ5nrCkGT10w92SxCCxI7qfeUpGelDhaujRvNTLZ_07G9dVV95U0EEJ0DVDQd1DPlo3GQLH8oqO6gXn2BIlJhz2axQY2e1zugcCMyft5cgE74J9KIWT7RtZWgPWiY7m686ph8uIU7I2YxZ4tyVLKDEkkjaKnIbHj2rJURZB4zuicvYJgT4YLe5yAxm2qefI8kzFQojuypzZNo8sQe22kIm9gkzxAW1-_WghAhgy2CuLhIDlrkook6L2OPmyis5c3ldm7q3Bo_gMX-dyjjazZZ0_HjOKeka9UXTu-Ol8JFDzDLTcmqCuITvpAuIZekmtlxswQ7Ri1",
                "2026-05-15T09:37:43",
            ),
            (
                "Service Delivery Manager",
                "Vidorra LLC",
                "http://click.monster.com/f/a/DcvnntUaDvLkldtRIQEH0A~~/AAAAARA~/lFcHubNPXtnd-D7FrZCTYI3qV-teCiBV4ijdtGu0Zc998-NCGaYflp2lx9PmAeyzUqIlpTEd_rn2fTxrAuL3HGDZTp9FNaA5Tx2tOVBVTTZ6OoArSjqr6acqL7Mmm-Y5-uYfsuk-o1IFms7b859NFpBhVoPuSByBO_-YZrXB0xLnqR_Xbqv8a3r_j78U4vK5PDYb0s2C50V7vA3LrU7z2TC0Mjq93cbvcUKNojv7sYuZVHbN-zLBYgHF1oQIbVZ4kmfxUkPn2AKF9xGWsiCu6VfhiWJcH4HGZXVKX8UyX_a5r3XEDcSJWie4c0ztA0gFXLbTVa4gjvUn0PTBBmRSAuCbXwPjdU3eM2RJNGA_64zEoa5OUlHE-T6A9h0d6DK2EGZVelm72gYwyFdauqdes0syzAKPiHwkvwT2nVhi7xI-kJ18GdONIQ5KPJX0CE8SnqNzALMGNoWzqxg94tfGrYkX6not0aVQh-uD44XvFAl3DjIaxCEjZrvXDXLj-DG1-4EHeb6METXPahdKXofBzuupLLNkHDutqD0aJYVqmS90necasdPcXlubUmGb8YdgkVsvqneJ1roNZPupT3iO_lXY8526w07oZ2c4wssnNtW1PlEkSUqsmNDWgN5LyaGcD3u1cEIs8LsMPJiBAlFVZ1uy0SS9Gfj8PqvUDietdjY~",
                "2026-05-15T09:37:43",
            ),
            (
                "Data Scientist/Engineer - Junior (Remote)",
                "SynergisticIT",
                "http://click.monster.com/f/a/pZo6Js3oFttaGnt43ZrZGg~~/AAAAARA~/g-0NXZDu_bdJnqQpfCICfCXo_rjW2OoNWO130qm0k9ne5dGiWu58H-_23CS_WIH3fqeS7_lDPsKoVBAmFh5X8-royFtypC1bWGDGcvs9K5iW2tSFEbR2XVrdJrcyHiuzvywmWD9Y3K1HYqbxFYBnbS-wWF3PXRqC-p7oONjhQiUR9rZq0O4ncisLVoXxZaPh86Ni_rGJztQ7tG_w4F46slb8OX_ARQpeF9CVDSJ2n3fXKay0wdkLUQoW7lkjd8MmRqjr06VNhV9vjO0_sf3CivCQD7nZ2Gv7OWyXItLci-qIPQWTcA2mO_i_EE7_9weQgWWwTi7ga8F-w3JX6bZ1bvJBEBAH-KV9g20L0KM0ijFgaCGqLk1hkkr3954GkPdrcEBcDDjzJGoNMCkTyo6aKeNQv6AoO5Ia5Xct6GbsYPlUebHxPSA4tQW9l6M2D2xqBVT9Oa2GPARj-m_fiZbeqtfZPEg4k4cbg1k5WlbAPhsD1P6BLUnspw0vuQ0Ioqd_AyQqY-t5a1PjkP32rBElt3_sPBk4yQ6iH6IF-e9BwACt2ellccwqSBa4sfP6EN9U8aXqIm--Co8n3a_nZC77kNmLq-Y9V76szAfWnEzu0wc2U69v0YoFqAwyBAOqeHcxpdTDmSZXA-lt2-z_TLIj8hS1zTZfGExAOHC9jsmNj4DClu-PY88Mu_c9GEOlRZileYQd4L8gP9g30IDhaGt_yA~~",
                "2026-05-15T09:37:43",
            ),
            (
                "Remote Tax Senior (International Tax) - Top 100 firm",
                "Jobot",
                "http://click.monster.com/f/a/8WgQTkDzmIWk4I7Hd7az0g~~/AAAAARA~/bt-k5gdjy1JbP2Y7Sxu-ju7fUnLkogaOhMmI6JH2rv9wuK8r8pptg4bY39k1McwVk-vhJcNXMAI9EJnFH_KA-p3Ca7cfwHvjFaDf3LFLQd-2LtucyKF5k-MlQ0OPTp2GYBQ5qrWabVy7HSSyg0taMPSisOy3W75S6Mx5f473sw_bQ5fu1CPw9cAHCMS1XkCKexvU7nZnri70oNmwMw72SoERW3u2zemXY1vYoRuYEI0ZKObooFQd3ErPI1NGG9mZQU_5o1G8EWYsaULQKgTTGlkYv6Y1dLwOM1DDMIb1VWMixj4WpgTkGCUJb0ScnCGMquLiuS4SJc8Eyyc0rHjmHK1fc0lZ63xPI-Ad2iiy0EDAaZZ3BW1l-ewFPNJhh6lh5CgfS6wWAK1CLtuZcmfrf5UCHpXuykwsV83dWqT8Mtl8R1Ui8BCYd1tb5QZ7aRHvEwkyTq1ykMIqECz81g5ZTz2U500UNYkrZchtJMEL4YDil_KJ6tXwRSo9wcQpj3MvFG2ghsJOjJgz6f0kjqR-B6WWVBBz21PB8FEe07GscZ5-bjXoYv6aHDQ04PQo9Lu-NbkaH7Gyx3tSdrLPBIvGY3F5SmMSEB3elu3tE7hwgDXxA1WIY6TrnxNsinpjcReIwpvR8WGCwY6Ki2BIlvEUcBHScVw63d-JNnBA9iIZfulCJXbH8fEeLg-EtxOz6Dv3XJIFIAnAHeD6v1Xd16q8ldXDGo1PaAlZrxIh5fJSJ4s~",
                "2026-05-15T09:37:43",
            ),
        ],
    },
    "noreply@jobright.ai": {
        "fixture": "jobright.eml",
        "jobs": [
            (
                "Product Data Scientist, Google Play, DSA",
                "Google",
                "https://jobright.ai/jobs/info/6a453e93c2d11a6a466687b1",
                "2026-07-01T16:37:39",
            ),
            (
                "Product Analytics Lead",
                "Napster Corp.",
                "https://jobright.ai/jobs/info/6a4541ee4f64ba41dcb4cbfb",
                "2026-07-01T16:37:39",
            ),
            (
                "Staff Analytics, Product & Marketing",
                "EarnIn",
                "https://jobright.ai/jobs/info/6a2b15dec07d4b6ae1c4921b",
                "2026-07-01T16:37:39",
            ),
            (
                "Lead Analyst (Supply Analytics, Bangkok-based, Relocation provided)",
                "Agoda",
                "https://jobright.ai/jobs/info/698e21a2f64d441a16505b3b",
                "2026-07-01T16:37:39",
            ),
            (
                "Senior Advisor, Business Analytics - Digital Product",
                "The Cigna Group",
                "https://jobright.ai/jobs/info/6a43f147ef17a815538a2589",
                "2026-07-01T16:37:39",
            ),
            (
                "Data Scientist 4/5 - Identity DSE",
                "Netflix",
                "https://jobright.ai/jobs/info/6a2a4ca00c4972328e7e826e",
                "2026-07-01T16:37:39",
            ),
        ],
    },
}

# Map each sender to one or more fixture files
FIXTURES_BY_SENDER = {
    "jobalerts-noreply@linkedin.com": [
        "linkedin_alert.eml",
        "linkedin_alert_2.eml",
        "linkedin_alert_3.eml",
        "linkedin_alert_4.eml",
    ],
    "jobs-noreply@linkedin.com": [
        "linkedin_jobs.eml",
        "linkedin_jobs_2.eml",
        "linkedin_jobs_3.eml",
        "linkedin_jobs_4.eml",
    ],
    "noreply@glassdoor.com": [
        "glassdoor_2.eml"
    ],  # glassdoor.eml needs scrubbing (contains personal data)
    "alert@indeed.com": [
        "indeed_alert.eml",
        "indeed_alert_2.eml",
        "indeed_alert_3.eml",
    ],
    "donotreply@match.indeed.com": [
        "indeed_match.eml",
        "indeed_match_2.eml",
        # indeed_match_3.eml and indeed_match_4.eml format not recognized by parser
    ],
    "no-reply@ziprecruiter.com": ["ziprecruiter.eml"],
    "no-reply@us.greenhouse-jobs.com": ["greenhouse.eml"],
    "hello@trueup.io": ["trueup.eml", "trueup_2.eml", "trueup_3.eml"],
    "monster@notifications.monster.com": [
        "monster.eml",
        "monster_2.eml",
        "monster_3.eml",
        "monster_4.eml",
    ],
    "noreply@jobright.ai": ["jobright.eml"],
}


def test_all_registered_senders_have_eml_fixtures():
    """Ensure every sender in SENDER_PARSERS has at least one fixture file."""
    fixture_senders = set(FIXTURES_BY_SENDER.keys())
    registered_senders = set(SENDER_PARSERS.keys())

    missing = registered_senders - fixture_senders
    extra = fixture_senders - registered_senders

    if missing:
        pytest.fail(f"Missing fixtures for senders: {missing}")
    if extra:
        pytest.fail(f"Extra fixtures for unregistered senders: {extra}")

    # Ensure each sender has at least one fixture
    for sender, fixtures in FIXTURES_BY_SENDER.items():
        assert len(fixtures) >= 1, f"Sender {sender} has no fixtures"


def test_golden_expected_stays_tied_to_fixture_inventory():
    """GOLDEN_EXPECTED must stay structurally tied to the on-disk fixture set so a
    live sender path cannot drift unguarded (cross-family review finding, #637).

    Two directions, both derived from state (SENDER_PARSERS / FIXTURES_BY_SENDER /
    disk) — never a hardcoded sender list:

    1. Every registered sender whose representative fixture EXISTS on disk must
       have a golden row. So dropping a real ``ziprecruiter.eml`` in later cannot
       add a sender to the live path without an exact-match golden guarding it.
    2. Every golden row must point at a fixture that is declared in
       FIXTURES_BY_SENDER and present on disk (no stale/renamed goldens).
    """
    fixtures_dir = Path(__file__).parent / "fixtures" / "emails"

    unguarded = [
        sender
        for sender in SENDER_PARSERS
        if any(
            (fixtures_dir / fixture_file).exists()
            for fixture_file in FIXTURES_BY_SENDER.get(sender, [])
        )
        and sender not in GOLDEN_EXPECTED
    ]
    assert not unguarded, (
        "Senders with an on-disk fixture but no GOLDEN_EXPECTED row "
        f"(add an exact-match golden entry): {unguarded}"
    )

    stale = []
    for sender, entry in GOLDEN_EXPECTED.items():
        fixture_file = entry["fixture"]
        if fixture_file not in FIXTURES_BY_SENDER.get(sender, []):
            stale.append((sender, fixture_file, "not declared in FIXTURES_BY_SENDER"))
        elif not (fixtures_dir / fixture_file).exists():
            stale.append((sender, fixture_file, "fixture file missing on disk"))
    assert not stale, f"GOLDEN_EXPECTED references unknown/absent fixtures: {stale}"


def _sender_fixture_cases() -> list[tuple[str, str]]:
    """Flatten FIXTURES_BY_SENDER into (sender, fixture_file) pairs for params."""
    return [
        (sender, fixture_file)
        for sender in SENDER_PARSERS
        for fixture_file in FIXTURES_BY_SENDER.get(sender, [])
    ]


@pytest.mark.parametrize(
    ("sender", "fixture_file"),
    _sender_fixture_cases(),
    ids=[f"{sender}:{fixture}" for sender, fixture in _sender_fixture_cases()],
)
def test_eml_fixture_round_trips_to_jobs(sender, fixture_file):
    """Each registered sender's real .eml fixture round-trips through the IMAP
    decode path to >=1 job with core fields populated.

    Parametrized per (sender, fixture) so a single missing fixture skips ONLY
    its own case. Previously this was one big loop with an inline ``pytest.skip``
    on the first missing file, so the absent ``ziprecruiter.eml`` silently
    disabled real-email validation for *every* sender.
    """
    fixtures_dir = Path(__file__).parent / "fixtures" / "emails"
    parser_func = SENDER_PARSERS[sender]
    fixture_path = fixtures_dir / fixture_file

    if not fixture_path.exists():
        pytest.skip(f"Fixture file not found: {fixture_path}")

    # Read .eml bytes and parse as RFC 5322 message
    with open(fixture_path, "rb") as f:
        eml_bytes = f.read()
    message = email.message_from_bytes(eml_bytes, policy=email.policy.default)

    # Simulate IMAP decode path
    imap_source = ImapSource()
    body = imap_source._extract_body(message)
    date = imap_source._extract_date(message)

    if not body:
        pytest.fail(f"Could not extract body from fixture: {fixture_path}")

    # Call the parser with decoded body
    jobs = parser_func(body, date or "")

    # Assert at least one job returned
    assert len(jobs) >= 1, f"No jobs parsed from fixture: {fixture_path}"

    # Assert core fields are populated
    for job in jobs:
        assert job.title, f"Job missing title from fixture: {fixture_path}"
        assert job.company, f"Job missing company from fixture: {fixture_path}"
        assert job.source, f"Job missing source from fixture: {fixture_path}"
        # Check for either source_url or url depending on model field
        assert job.source_url or getattr(job, "url", None), (
            f"Job missing source_url/url from fixture: {fixture_path}"
        )


def _golden_fixture_cases() -> list[tuple[str, str]]:
    """Flatten GOLDEN_EXPECTED into (sender, fixture_file) pairs for params."""
    return [(sender, entry["fixture"]) for sender, entry in GOLDEN_EXPECTED.items()]


@pytest.mark.parametrize(
    ("sender", "fixture_file"),
    _golden_fixture_cases(),
    ids=[f"{sender}:{fixture}" for sender, fixture in _golden_fixture_cases()],
)
def test_eml_fixture_round_trips_to_golden_expected(sender, fixture_file):
    """Each registered sender's representative fixture round-trips through the IMAP
    decode path to the exact canonical job list pinned in GOLDEN_EXPECTED.

    This is the safety net for the Gmail-decommission refactor: it catches
    body-extraction regressions that produce *wrong-but-populated* jobs, which
    the non-empty assertion would silently pass.

    Parametrized per (sender, fixture) so one sender's failure isolates to its
    own case.
    """
    fixtures_dir = Path(__file__).parent / "fixtures" / "emails"
    parser_func = SENDER_PARSERS[sender]
    fixture_path = fixtures_dir / fixture_file

    if not fixture_path.exists():
        pytest.skip(f"Fixture file not found: {fixture_path}")

    # Read .eml bytes and parse as RFC 5322 message
    with open(fixture_path, "rb") as f:
        eml_bytes = f.read()
    message = email.message_from_bytes(eml_bytes, policy=email.policy.default)

    # Simulate IMAP decode path
    imap_source = ImapSource()
    body = imap_source._extract_body(message)
    date = imap_source._extract_date(message)

    if not body:
        pytest.fail(f"Could not extract body from fixture: {fixture_path}")

    # Call the parser with decoded body
    jobs = parser_func(body, date or "")

    # Build canonical tuples from actual jobs
    actual_jobs = [
        (
            job.title,
            job.company,
            job.source_url or getattr(job, "url", None),
            # Full naive-UTC datetime (not just .date()): pins _extract_date's exact
            # output so a same-day tz/clock regression can't slip through. The time
            # is deterministic (from the fixed .eml Date header), so no flakiness.
            job.posted_date.isoformat() if job.posted_date else None,
        )
        for job in jobs
    ]

    # Assert exact match with golden expected
    expected_jobs = GOLDEN_EXPECTED[sender]["jobs"]
    assert actual_jobs == expected_jobs, (
        f"Job list mismatch for {sender} / {fixture_file}\nExpected: {expected_jobs}\nActual: {actual_jobs}"
    )


def test_email_fixtures_do_not_contain_obvious_pii():
    """Ensure .eml fixtures are scrubbed of personal data."""
    fixtures_dir = Path(__file__).parent / "fixtures" / "emails"

    if not fixtures_dir.exists():
        pytest.skip("Fixtures directory not found")

    scanned = list(fixtures_dir.glob("*.eml"))
    assert scanned, "No .eml fixtures found to scan — PII guard would pass vacuously."
    for fixture_path in scanned:
        with open(fixture_path, encoding="utf-8") as f:
            content = f.read()

        # Check for To: headers
        for line in content.split("\n"):
            if line.strip().startswith("To:"):
                pytest.fail(f"Fixture contains To: header: {fixture_path}")

        # Check for personal identifiers (kept in sync with job_finder/sources/_pii_scrub.py)
        from job_finder.sources._pii_scrub import DEFAULT_DENYLIST

        denylist = list(DEFAULT_DENYLIST)
        for identifier in denylist:
            if identifier.lower() in content.lower():
                pytest.fail(
                    f"Fixture contains disallowed identifier '{identifier}': {fixture_path}"
                )

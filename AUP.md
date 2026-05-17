# Acceptable Use Policy

This is open-source software you run on your own machine. There is no
service to abuse and no central operator to police use. What follows is
an advisory list of behaviors that, while mechanically possible, make
you a bad actor in the open-source commons. There is no enforcement
mechanism — this is a calibration, not a contract.

## Don't

- **Don't run the careers-page crawler at scale against sites that
  forbid it.** The crawler tier (Greenhouse / Lever / Ashby /
  SmartRecruiters / Workday + the tier-4 AI navigator) issues outbound
  HTTP fetches. You are responsible for honoring `robots.txt` and
  per-site terms of service. The default scan cadence (configurable
  in `config.yaml`) is conservative; raising it aggressively against
  a single domain is your call and your liability.
- **Don't violate ATS terms of service.** Greenhouse, Lever, Ashby,
  SmartRecruiters, and Workday have their own ToS for board scraping.
  Read them. The app does not check them for you.
- **Don't target individuals or specific companies with adversarial
  intent.** The pipeline detector, AI navigator, and email parsers are
  built for personal job search — not for harassment, dossier-building,
  or scraping someone's hiring funnel against their wishes.
- **Don't redistribute scraped data.** What lands in your local SQLite
  is yours operationally; republishing scraped postings, candidate
  profiles, or company internals is a separate legal question — usually
  with a worse answer than you'd hope.

## Yes

- **Yes, use it for your own job search.** That is the entire point.
- **Yes, fork it, modify it, share it.** AGPL-3.0 — see `LICENSE`.

If a use case feels ambiguous, default to the more conservative
interpretation. There is no enforcement, but there is reputation.

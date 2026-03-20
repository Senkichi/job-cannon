# Resume Generation Guidelines

> Reference document for AI-assisted resume tailoring. These rules are derived from extensive iterative refinement and represent hard-won quality standards. Follow them strictly.

---

## 1. Source Fidelity: Never Fabricate

**This is the single most important rule.** The resume generator must only include skills, tools, and experiences that exist in the candidate's source material (knowledge base / experience reference).

### What this means in practice

- If the JD asks for Looker and the candidate only has Tableau and Mixpanel, list Tableau and Mixpanel. Do NOT add Looker.
- If the JD asks for dbt and the candidate uses custom ETL pipelines, describe the ETL work. Do NOT add dbt.
- If the JD asks for Snowflake and the candidate uses BigQuery, list BigQuery. Do NOT add Snowflake.
- Equivalent tools can be positioned as analogs through bullet context (e.g., a bullet about building dashboards in Tableau implicitly covers "dashboard experience" even if the JD names Looker), but the Skills section must never list a tool the candidate hasn't used.

### Gap mitigation strategy

When the JD requires a tool or experience the candidate lacks, the correct approach is:

1. Identify the closest analog in the candidate's actual experience
2. Lead with the real tool/experience in bullets, framed to address the same underlying competency
3. In the Skills section, list only real tools
4. Let the bullet context do the bridging work; do not fabricate line items

---

## 2. Professional Summary

### Structure

3-4 sentences maximum. Follow this formula:

1. **Sentence 1:** [Role archetype] with [X]+ years of experience [doing what], [in what context].
2. **Sentence 2:** Proven track record [strongest achievement pattern], including [one concrete example with a number].
3. **Sentence 3:** Brings [2-3 specific capabilities from the JD] and [forward-looking value prop for this role].

### Rules

- Lead with years of experience and role archetype (e.g., "Product Data Scientist with 8+ years...")
- Include 2-3 of the JD's top keywords naturally, not as a keyword dump
- End with a forward-looking statement about what the candidate brings to THIS specific role
- Keep it to 3-4 lines when rendered in the document. If it exceeds 5 lines, it's too long; cut.
- Do not use the word "seeking" or frame the candidate as a job-seeker. Frame them as a practitioner bringing value.
- Mirror the JD's title or archetype language in the opening (e.g., if the JD says "Data Scientist III, Product Analytics," the summary should open with phrasing that echoes that framing)

### Anti-patterns

- Wall of text trying to cover every skill area
- Generic summaries that could apply to any data role
- Keyword stuffing disguised as sentences
- Repeating the same concept twice with different words to hit more keywords

---

## 3. Skills Section

### Formatting

Use a compact, scannable format. Pipe-separated or category-labeled. Example:

```
Experimentation Design (A/B Testing, RCTs) | Causal Inference (DiD, Mixed Effects, ITT) | SQL (BigQuery, PostgreSQL) | Python (Pandas, SciPy, Statsmodels) | Tableau | Mixpanel
```

### Rules

- **Hard skills and methodologies ONLY.** Never list soft skills like "Cross-Functional Collaboration," "Stakeholder Communication," "Team Leadership & Mentorship," or "Executive-Level Presentation." These belong in experience bullets, demonstrated through action.
- **Front-load to the JD.** Reorder skills so the JD's most prominent requirements appear first. If the JD leads with experimentation, lead with experimentation. If it leads with SQL, lead with SQL.
- **One to two lines maximum.** If the skills section exceeds two lines, cut the least relevant items.
- **No fabrication.** Only list tools the candidate has actually used. See Rule 1.

---

## 4. Bullet Writing

### Formula

Every bullet must follow: **Action Verb + What You Did + How/With What + Quantified Impact**

### Construction rules

- **Lead with strong action verbs:** Designed, Engineered, Architected, Directed, Defined, Executed, Led, Built, Partnered, Established. Rotate verbs across bullets; never start two consecutive bullets with the same verb.
- **Quantify aggressively:** User counts, revenue impact, percentage improvements, time savings, ROI, team sizes (when appropriate), client counts. If a bullet has no number, it needs a compelling reason to exist.
- **1-2 lines per bullet.** Absolute maximum of 3 lines, and only in rare cases where the achievement genuinely requires it. If a bullet exceeds 2 lines, look for words to cut first.
- **Past tense throughout**, including the current role. It reads more consistently.

### The "so what?" test

Every bullet must pass this test: after reading it, a hiring manager should immediately understand why it matters. If the answer to "so what?" is unclear, the bullet needs a result clause or should be cut.

**Passes:** "Designed and executed a Randomized Controlled Trial that validated a 245% lift in appointment booking conversion and 350% ROI, directly informing FY26 workforce planning and resource allocation strategy."

**Fails:** "Conducted post-experiment analysis using Python (Chi-squared tests, Logistic Regression) to verify statistical significance and control for confounding variables." ← Methods listing with no business outcome.

### Anti-patterns to eliminate

#### 1. The "Problem-Identified" opener
**Bad:** "Identified lack of rigorous outcome measurement for AI Chat product launch and defined a two-phase measurement strategy..."
**Good:** "Defined the measurement strategy for the AI Chat product feature, implementing a two-track approach: a 30-day A/B test for leading engagement indicators and a 9-month Difference-in-Differences study to measure downstream health outcomes."

The problem-setup ("Identified lack of...", "Recognized that...", "Diagnosed fragmented...") burns half the bullet on context the reader doesn't need. Lead with the action and the deliverable. Use this pattern sparingly (at most once per role), not as the default framing for every bullet.

#### 2. Methods-listing without business outcome
**Bad:** "Conducted Chi-squared tests and Logistic Regression to verify statistical significance and control for confounding variables (member age, risk score)."
**Good:** Fold the methods into a bullet that has a business result, or cut entirely if another bullet already demonstrates statistical rigor.

#### 3. Redundant experimentation bullets
If two bullets both demonstrate "can design experiments," replace one with a bullet showing a different dimension: strategic scope, delegation, infrastructure work, stakeholder negotiation, pipeline engineering, etc.

#### 4. Soft skill claims as standalone bullets
**Bad:** "Managed stakeholder relationships across Product, Engineering, and Sales organizations."
**Good:** Demonstrate stakeholder management through a concrete deliverable: "Partnered with Sales and Engineering VPs to design a client-safe A/B testing protocol for the AI Chat rollout, balancing statistical rigor with enterprise client risk tolerance."

### Bullet selection by role seniority

- **Current/most recent role (Lead/Senior level):** 4-6 bullets. These carry the resume.
- **Previous role at same company:** 2-3 bullets. Show progression and different capabilities.
- **Prior companies (mid-career):** 1-2 bullets each. Concise, high-impact only.
- **Early career:** 1 bullet maximum. Only include if it adds a dimension not shown elsewhere.

---

## 5. Confidentiality and Disclosure

### Client names
Never include specific client names in resume bullets. Use generic descriptors:
- "a major enterprise client" 
- "enterprise health plan clients"
- "a Fortune 500 financial services client"

Client names may exist in the source material for context but must never surface in the output document.

### Team sizes
Omit specific team sizes when they could trigger premature disqualification. Instead of "led a team of 4 analysts," write "led a team of senior analysts." This lets the interview conversation happen; a specific number might prevent it.

### Exception
If a JD explicitly requires experience managing teams of a specific size and the candidate meets the threshold, specific numbers can be included.

---

## 6. Formatting and Style

### Document-level rules
- **2 pages maximum.** 1.5 pages is ideal for most roles.
- **Reverse chronological** within Professional Experience.
- **ATS-friendly:** Standard section headers, no graphics, no columns, no text boxes.
- **US Letter format** (not A4).

### Typography rules
- **No bold text within bullet points.** Bold is reserved for: section headers, company names, job titles, and project titles only.
- **No em dashes anywhere in the document.** Restructure sentences using commas, semicolons, or separate clauses.
- **Minimize parentheses.** Integrate details naturally. Write "using Random Forest modeling" not "using statistical modeling (Random Forest)."
- **Do not define well-known acronyms.** ITT, DiD, RCT, ROI, KPI, ETL, CAC, LTV do not need expansion.
- **Do not include unnecessary technical specificity in parentheses.** For example, do not write "(10,000 iterations)" after mentioning Monte Carlo simulation.

### Section headers (required, in order)
1. Header (Name, Location, Phone, Email, LinkedIn)
2. Professional Summary
3. Skills (or "Technical Skills")
4. Professional Experience
5. Key Projects & Leadership Highlights (optional; include for Staff/IC-heavy roles)
6. Education

### Education
- Keep brief: degree, university, location
- No GPA, no coursework details, no graduation dates (to avoid age bias)
- If the candidate has a thesis relevant to the role, include a one-line thesis description

### Date accuracy
Validate all employment dates against the source material. Do not approximate or round. If the source says "May 2024," the resume must say "May 2024," not "Feb 2024."

---

## 7. JD Mirroring and ATS Optimization

### Keyword strategy
- Extract the top 5-7 keywords/themes from the JD
- Ensure each appears at least once in the resume (summary, skills, or bullets)
- Use the JD's exact terminology for tools and methodologies (e.g., if the JD says "experimentation" not "experiments," use "experimentation")

### Mirroring calibration
- Mirror the JD's keywords strategically, not reflexively
- Never lift full phrases verbatim from the JD, especially from requirements or "day in the life" sections
- If a phrase sounds like it was copied from the posting, rework it
- Use a JD phrase once at most if it adds natural flavor; never repeat the same JD phrase multiple times across the resume
- The reader should feel alignment, not pattern-matching

### Standard section headers
Use exactly these names (or close variants) for ATS parsing:
- "Professional Summary" (not "Objective," not "About Me")
- "Skills" or "Technical Skills" (not "Core Competencies," not "Toolkit")
- "Professional Experience" or "Experience" (not "Work History," not "Career")
- "Education" (not "Academic Background")

---

## 8. Structural Decisions by Role Archetype

### Senior/Staff Data Scientist (IC-heavy)
- Include Key Projects section to demonstrate depth and ownership
- Lead bullets with experimentation, causal inference, statistical methods
- Skills section front-loads methodologies and programming languages
- Professional Summary emphasizes technical sophistication and product impact

### Staff/Senior Product Analyst
- Key Projects section optional (include if the candidate has signature analytical projects)
- Lead bullets with KPI definition, cross-functional partnership, business outcomes
- Skills section front-loads analytics tools and product instrumentation
- Professional Summary emphasizes business impact and stakeholder influence

### Analytics Manager / Senior Manager
- Omit Key Projects section unless the role is a player-coach hybrid
- Lead bullets with team leadership, then technical execution
- Skills section can be shorter; leadership is demonstrated through bullets
- Professional Summary emphasizes team building, strategic planning, and organizational impact

---

## 9. Post-Generation Quality Checks

Run these checks on every generated resume before delivering:

### Content integrity
- [ ] No skills listed that the candidate doesn't actually have
- [ ] No client names appear anywhere in the document
- [ ] No specific team sizes unless strategically appropriate
- [ ] All dates match the source material exactly
- [ ] Education section is complete and correctly populated

### Structural checks
- [ ] Document is 2 pages or fewer
- [ ] All required sections are present and in order
- [ ] Skills section is 1-2 lines maximum
- [ ] Professional Summary is 3-4 sentences (not a wall of text)
- [ ] Most recent role has 4-6 bullets; earlier roles have progressively fewer

### Style checks
- [ ] No bold text within bullet point content
- [ ] No em dashes anywhere in the document
- [ ] No unnecessary parenthetical definitions
- [ ] No passive voice ("was responsible for," "was involved in")
- [ ] No vague language ("helped with," "assisted in")
- [ ] No two consecutive bullets start with the same verb
- [ ] Every bullet has a quantified result or compelling business outcome
- [ ] No bullet exceeds 3 lines; most are 1-2 lines

### JD alignment checks
- [ ] Top 5 JD keywords each appear at least once
- [ ] No JD phrase is repeated more than once across the resume
- [ ] No verbatim lifts from the JD's requirements section
- [ ] Skills are reordered to match JD priorities
- [ ] Professional Summary mirrors the JD's role archetype

### Readability check
- [ ] Every bullet passes the "so what?" test
- [ ] The resume reads like a human wrote it, not like an AI expanded a prompt
- [ ] No filler clauses that can be cut without losing meaning

---

## 10. Common Failure Modes (Ranked by Severity)

1. **Fabricating skills or tools** to match the JD. This is resume fraud and will be caught in interviews.
2. **Broken or missing sections** (e.g., Education showing "UNKNOWN"). Validate all sections are populated.
3. **Incorrect dates.** Always validate against source data.
4. **Soft skills in the Skills section.** These belong in bullets, demonstrated through action.
5. **Bloated Professional Summary** trying to cover every possible keyword. Keep it tight.
6. **"Problem-identified" bullet pattern overuse.** Once per role maximum; lead with action verbs by default.
7. **Explicit team sizes** that could trigger leveling disqualification.
8. **Em dashes in bullets.** Restructure the sentence instead.
9. **Bullet verbosity.** If it's 3+ lines, it needs trimming.
10. **Missing Key Projects section** for IC-heavy roles where depth of work matters.

# Alpine Government Mechanics

A short reference for how Alpine, Wyoming and the surrounding Lincoln County government structures work, what cadence they meet on, and what laws govern public access to their records and meetings. Every claim below has an inline source URL; statute citations link to wyoleg.gov, Alpine specifics link to alpinewy.gov, and county specifics to lincolncountywy.gov.

This doc is part of the Government Watchdog Phase 1 deliverables (see [phase1-spec.md §2.4](./phase1-spec.md)). It is not a legal reference; it is a working reference for the pipeline that downloads, transcribes, and indexes Alpine's public record.

## 1. Alpine's classification under Wyoming law

Wyoming municipalities are organized under [Title 15 of the Wyoming Statutes](https://wyoleg.gov/statutes/compress/title15.pdf) ("Cities and Towns"). The state recognizes two operational classes by population:

- **First-class cities** — population 4,000 or greater. Governed under W.S. §15-1-101 through §15-1-118 ([Title 15, Chapter 1](https://wyoleg.gov/statutes/compress/title15.pdf)).
- **Towns** (incorporated municipalities under 4,000) — same chapter, with additional provisions specific to towns.

Alpine is a town. Its own annual filings to the Wyoming Department of Audit use the form titled **"Local Government Annual Report — For Towns under 4,000 population"** (e.g. [FYE 6-30-2019 report](https://www.alpinewy.gov/media/1361), document id `documents.id=3` in `Database/gov_watchdog.db`). Alpine's 2020 census population was 1,156 people ([U.S. Census Bureau QuickFacts: Alpine town, Wyoming](https://www.census.gov/quickfacts/fact/table/alpinetownwyoming/POP010220)), which keeps it well within the town threshold for the foreseeable future.

Wyoming towns are governed by a **mayor and town council** (W.S. §15-1-103 et seq., [Title 15 Ch. 1](https://wyoleg.gov/statutes/compress/title15.pdf)). The mayor is the chief executive; the council is the legislative body. Alpine's elected officials are listed on the [Town's Mayor & Council page](https://www.alpinewy.gov/mayor-council).

## 2. Charter form and ordinance/resolution practice

Alpine acts as a **statutory town** — it does not appear to have adopted a separate home-rule charter; it operates directly under Title 15. Statutes vest legislative power in the council via the ordinance process (W.S. §15-1-114, ["ordinances; passage; publication"](https://wyoleg.gov/statutes/compress/title15.pdf)).

Practical evidence in the corpus:

- Numbered ordinances appear in town files using the convention `ORDINANCE NO. <year>-<sequence>`, e.g. **Ordinance 2019-03 — Town of Alpine Budget for Fiscal Year 2018/2019** ([source](https://www.alpinewy.gov/media/1366), `documents.id=2`). This matches the W.S. §15-1-114 publication and numbering pattern.
- Resolutions and budget amendments are passed by the council in open session under the same ordinance machinery.

The town's ordinance code, when codified, is hosted on Municode at [library.municode.com/wy/alpine](https://library.municode.com/wy/alpine). That is one of Phase 1's crawl targets ([phase1-spec.md §2.1](./phase1-spec.md)).

## 3. Meeting cadence

Wyoming towns must hold council meetings on a publicly fixed schedule and post notice (Open Meetings Act, see §4 below). Cadence specifics are set by each town's own rules.

Alpine publishes its meeting schedule on the [Town Council Meetings & Agendas page](https://www.alpinewy.gov/town-council). Typical recent practice:

- **Town Council** — regular meetings the **third Tuesday of each month at 6:00 p.m.** at Alpine Civic Center, with special meetings called as needed ([alpinewy.gov/town-council](https://www.alpinewy.gov/town-council); cross-checked against agenda PDFs in `Raw-PDFs/2026/alpinewy/`).
- **Planning & Zoning Commission** — convenes monthly when there is business; agendas are posted on the [Planning & Zoning page](https://www.alpinewy.gov/planning-zoning).
- **Special and emergency meetings** are noticed on the front page of [alpinewy.gov](https://www.alpinewy.gov/) and (per W.S. §16-4-404(c)) given to media outlets that have requested notice.

Lincoln County's commission meets in **Kemmerer** (county seat) on its own schedule, separately from the Alpine Town Council. Alpine-relevant county actions (e.g. road maintenance affecting the Alpine corridor, county-wide ordinances) appear in commission agendas published at [lincolncountywy.gov](https://www.lincolncountywy.gov/). Phase 1 only follows Alpine-relevant subpages of the county site (see [phase1-spec.md §2 / §6](./phase1-spec.md), `lincoln` target with `alpine_filter=True`).

## 4. Public-records access — Wyoming Public Records Act

The Wyoming Public Records Act (WPRA) is codified at [W.S. §16-4-201 through §16-4-205](https://wyoleg.gov/statutes/compress/title16.pdf) (Title 16, Chapter 4, Article 2). Key points relevant to the watchdog pipeline:

- **§16-4-202(a)** — all public records of a state or local agency are open for inspection by any person at reasonable times, except as otherwise provided by law.
- **§16-4-203(d)** — the official custodian must respond within **thirty (30) calendar days** of a request (extended response window adopted in the 2019 amendments to the WPRA; the prior "as soon as practicable" standard is gone).
- **§16-4-204** — fees may be charged for actual production cost; redaction of statutorily exempt material is mandatory before release.
- **§16-4-203(g)** — denials must cite the specific statutory basis and may be reviewed by the district court.

Alpine accepts requests via a printable **PUBLIC RECORDS REQUEST** form ([source](https://www.alpinewy.gov/media/12011), `documents.id=1`). The form routes to the town clerk's office at the Alpine Civic Center ([contact info](https://www.alpinewy.gov/contact)).

## 5. Open-meetings access — Wyoming Public Meetings Act

The Wyoming Public Meetings Act (WPMA) is codified at [W.S. §16-4-401 through §16-4-408](https://wyoleg.gov/statutes/compress/title16.pdf) (Title 16, Chapter 4, Article 4). Watchdog-relevant points:

- **§16-4-403** — all meetings of an "agency" (defined to include municipal governing bodies) shall be open to the public; an "action" of a closed meeting is null and void unless ratified in open session.
- **§16-4-404(b)** — agendas and minutes are public records under the WPRA; minutes must be available for inspection within a reasonable time after the meeting.
- **§16-4-405** — executive (closed) sessions are limited to enumerated subjects (personnel, pending litigation, security, real-estate negotiation, etc.); the body must vote in open session to enter executive session and state the statutory ground.
- **§16-4-404(c)** — for special meetings the body must give written or oral notice at least **eight (8) hours** in advance to the news media that have requested it, and post notice in a prominent place.

Alpine's published meeting agendas (Phase 1 corpus) include a standard "Action Items" section followed by "Public Comment" — consistent with the WPMA requirement that members of the public have an opportunity to address the body on agenda items.

## 6. Cross-references inside this repo

- **Phase 1 spec** — [Docs/phase1-spec.md](./phase1-spec.md)
- **Crawler target list** — `scripts/crawl_pdfs.py::TARGETS`
- **Schema** — `Database/migrations/0001_init.sql`
- **Live acceptance log** — `Logs/acceptance.log` (most recent run: 16 PDFs, 9 transcripts indexed)

## 7. Open items / Phase 2 follow-ups

1. **Ordinance code coverage on Municode.** As of the Phase 1 crawl the Alpine entry on Municode is an HTML browser; downloadable code PDFs may not exist. Treat the HTML capture as the canonical Phase 1 artifact; revisit if Municode publishes PDF exports ([phase1-spec.md §8 Q2](./phase1-spec.md)).
2. **Meeting-date enrichment.** `transcripts.meeting_date` and `meetings.meeting_date` are NULL until a Phase 2 enrichment pass derives them from titles + filenames ([phase1-spec.md §8 Q3](./phase1-spec.md)).
3. **Lincoln County coverage.** Phase 1 follows only Alpine-relevant county pages; expanding to a full county feed is a deliberate scope expansion and should be a separate WEI ticket if the watchdog grows.
4. **Charter verification.** This doc treats Alpine as a statutory town under Title 15. If a separate municipal charter is adopted later, this section needs an update with the charter URL and adoption ordinance.

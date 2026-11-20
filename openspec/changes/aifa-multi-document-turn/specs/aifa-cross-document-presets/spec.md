# Spec Delta

## Purpose

Lets the planner get cross-document comparison tables (retrospective, peer comparison, cross-source
verification) that the service computes in code from metric facts already in the turn's Case. The
rules are RFA's comparison policy (`comparison_policy_v1`). The model comments on numbers it is
shown instead of computing them.

## ADDED Requirements

### Requirement: Presets are computed from Case metric facts named by id

`aifa_compare` SHALL take `preset` (`retrospective`, `peer_comparison` or
`cross_source_verification`) and `artifact_ids`: 2 to 20 ids of metric facts in this turn's Case,
meaning extracted, calculated or user-provided metric evidence. It SHALL be read-only and answer
without waiting for changing calls. Ids not in the Case, or not metric facts, SHALL be listed as
missing; with fewer than two found it SHALL refuse with `нет_входов`.

#### Scenario: Unknown preset

- **WHEN** the planner calls `aifa_compare(preset="ranking", artifact_ids=[...])`
- **THEN** the answer is `ОТКАЗ неверные_аргументы`

#### Scenario: Facts from another Case

- **WHEN** the planner names ids that exist only in the Case of another folder
- **THEN** they are reported missing, and with fewer than two found facts the answer is `ОТКАЗ нет_входов`

#### Scenario: Available while a move runs

- **WHEN** an `aifa_move` of the same turn is executing and the planner calls `aifa_compare`
- **THEN** `aifa_compare` answers without waiting for the move

### Requirement: Fact attributes have one source each

For each fact, `aifa_compare` SHALL take:
- the metric, period, value, unit and basis from the fact itself;
- the company (key and name) and the source type from the fact's document (matched by document
  version), with the meta card before registry fields;
- the accounting standard and company type from the profile recorded when a loop opened on that
  document.

None of these SHALL come from call arguments. A fact whose document is no longer active in the
Case SHALL be excluded, with that reason.

#### Scenario: Company resolved through the document

- **WHEN** fact `e1` came from a document whose meta card names «Сбербанк», and fact `e2` from one
  naming «ВТБ»
- **THEN** the peer table labels `e1` with Сбербанк and `e2` with ВТБ

### Requirement: Comparisons are independent of argument order

Grouping, exclusions and every computed figure SHALL be the same for any order of the same
`artifact_ids`. Within a metric group, facts SHALL be compared only within the same unit, scale
(after normalisation) and basis. A group's comparable subset SHALL be the largest one, with ties
broken by unit name.

#### Scenario: Reordered arguments

- **WHEN** the same five ids are passed in two different orders
- **THEN** both answers are identical

#### Scenario: Different currencies

- **WHEN** a peer comparison holds two RUB facts and one USD fact for the same metric
- **THEN** the USD fact is excluded with reason "несопоставимая единица", and the RUB facts are compared

### Requirement: Materiality follows RFA's comparison policy

The status of a difference SHALL depend on the metric kind:
- **Percent metrics:** material when the difference is at least 1 percentage point and the
  relative difference is at least 5%.
- **Money metrics:** material when the relative difference is at least 5% and the absolute
  difference is at least 1% of the company's size benchmark.
- **Unknown threshold:** when it cannot be decided, the status SHALL be `minor_difference`.

The benchmark SHALL be the largest absolute value among the named facts of the same company,
basis and period:
- `TOTAL_ASSETS` for a bank or a point-in-time metric;
- otherwise `SALES_REVENUE`, then `TOTAL_ASSETS`.

#### Scenario: Six percent apart with a benchmark

- **WHEN** two sources give net profit 100 and 106, and the named facts include `TOTAL_ASSETS` = 500
  for the same company, basis and period
- **THEN** the status is `material_difference`

#### Scenario: Two percent apart

- **WHEN** two sources give 100 and 102 for a money metric with a benchmark present
- **THEN** the status is `minor_difference`

#### Scenario: No benchmark among the facts

- **WHEN** two sources give 100 and 110 for a money metric, and no `TOTAL_ASSETS`/`SALES_REVENUE`
  fact of that company is named
- **THEN** the status is `minor_difference`, with a note that the threshold is unknown

### Requirement: Retrospective builds one series per company and metric

`retrospective` SHALL group facts by company key and metric, with one point per period. The
latest point selects the period shape: quarters compare only with quarters, point-in-time values
only with point-in-time values, and year-to-date only with the same span. Per series it SHALL
return:
- values in chronological order;
- the latest point vs the previous one (45–120 days earlier) and vs a year earlier (320–410 days);
- the first comparable point vs the latest, with CAGR from calendar time where the policy allows
  it;
- the trend points.

#### Scenario: Three years of net profit

- **WHEN** facts give Сбербанк net profit for 2022, 2023 and 2024
- **THEN** the table lists three values in period order, the 2022→2024 change, and the trend points

#### Scenario: Two companies give two series

- **WHEN** the facts give net profit of Сбербанк and ВТБ for 2023 and 2024
- **THEN** the table has one series per company

#### Scenario: A quarter does not chain onto a year

- **WHEN** the latest point is Q3 2024 and the other points are FY 2022 and FY 2023
- **THEN** no quarter-over-quarter or first-to-latest change is computed from the annual points

### Requirement: Peer comparison covers several companies period by period

`peer_comparison` SHALL accept facts of at least two companies. It SHALL build rows per period
and list gaps grouped by company: each company with the metrics it is missing. A missing value
SHALL never be shown as zero. Before comparing a metric, it SHALL check that the companies share
standard, currency, scale and basis, and SHALL exclude those that do not, with the reason. Per
metric and period it SHALL show each company's value and a neutral rank (1 = largest), with no
wording that judges a company better or worse.

#### Scenario: Two banks in 2024

- **WHEN** facts give net profit for Сбербанк and ВТБ, both IFRS, both for 2024
- **THEN** the table lists both values with ranks 1 and 2 and no evaluative wording

#### Scenario: Different standards

- **WHEN** one company's fact comes from an IFRS report and the other's from an RSBU report
- **THEN** the metric row excludes one of them with reason "разные стандарты"

#### Scenario: A peer missing a period

- **WHEN** Сбербанк has net profit for FY 2023 and FY 2024, and ВТБ only for FY 2024
- **THEN** there are rows for FY 2023 and FY 2024, and the gaps list ВТБ with the missing metric,
  not a zero

#### Scenario: No period in common

- **WHEN** Сбербанк has only FY 2023 and ВТБ only FY 2024
- **THEN** the table still has two rows and two gap entries, not an empty answer

### Requirement: Cross-source verification picks no winner

`cross_source_verification` SHALL accept facts of one company and one period from at least two
source types. Per company, metric, period and basis, it SHALL:
- find agreements: an identical value reported by at least two different documents;
- when the values differ, compute one spread between the smallest and the largest value, as the
  absolute difference divided by the larger absolute value, with the benchmark taken for the
  largest value's row.

Each metric SHALL get one status: `incompatible_unit` when units cannot be compared, else a
difference status under the materiality rule, else `agreement`.

#### Scenario: Press release agrees with the report

- **WHEN** the report and the press release give the same net profit after scale normalisation
- **THEN** the metric has status `agreement`

#### Scenario: Three sources, spread from the extremes

- **WHEN** the report says 100, the press release 103 and the presentation 107 for one metric
- **THEN** one spread is computed between 100 and 107, and no source is marked as correct

#### Scenario: Different unit kinds

- **WHEN** the presentation reports a metric in percent and the report in RUB
- **THEN** the metric has status `incompatible_unit`, and no difference is computed

### Requirement: Every number in the table cites its fact

Each value in an `aifa_compare` answer SHALL be shown with the artifact id of the fact it came from.
Each computed figure (change, rank, trend, spread) SHALL be shown with the ids of the facts it was
computed from. The answer SHALL fit the result limit; a longer table SHALL be cut by whole rows,
with a note naming the rows left out.

#### Scenario: Cited cross-source row

- **WHEN** a cross-source metric has `e1` (report) as its smallest value and `e2` (press release)
  as its largest
- **THEN** the row shows `e1` and `e2` beside their values and beside the spread

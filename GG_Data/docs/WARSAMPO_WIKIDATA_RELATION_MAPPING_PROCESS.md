# WarSampo–Wikidata relation-type mapping process

Status: proposed complete-coverage method, 11 October 2026. The earlier
[Finnish WWII POC](../poc/relation_alignment/README.md) established one scoped
mapping. This document defines how to examine **every available relation-type
overlap in a frozen two-source scope**. It does not claim that all mappings
have already been reviewed. It does not extract or import facts into the
Pacific graph.

## 1. Is EventKG required?

**No, for WarSampo ↔ Wikidata predicate alignment.** Both sources expose their
own identities, predicates, definitions, and statements. EventKG integrates
Wikidata and other sources, so an EventKG edge from its Wikidata named graph
cannot independently confirm a Wikidata edge. Adding EventKG now would add
identity and provenance work without resolving the first two-source question.

EventKG can help later by providing a named-event inventory, `owl:sameAs`
bridges, event/place/time properties, and source-specific named graphs. Its
reified relations also carry roles and temporal context. These features may
help with WarSampo events that lack direct Wikidata links. They must be checked
at the exact event granularity: a short command-change event is not identical
to the war that contains it. [EventKG's tutorial](https://eventkg.l3s.uni-hannover.de/tutorial.html)
explains direct event properties, reified relations, and named graphs. The
existing EventKG-centered project memos address a broader graph architecture;
this two-source mapping step does not change that architecture. EventKG's
public query service returned HTTP 500 during the POC, so it must not block
this step.

## 2. Define the finite scope before counting

Freeze a WarSampo endpoint/dump version or retrieval date, the [WarSampo
schema](https://github.com/SemanticComputing/Warsampo-schema), a Wikidata
revision or retrieval date, and the [CIDOC CRM version](https://cidoc-crm.org/html/cidoc_crm_v7.1.3.html)
used to interpret CIDOC properties. Record the exact SPARQL/API queries and
source URLs. Wikidata's [entity API](https://www.wikidata.org/wiki/Wikidata:Data_access)
provides complete claims with ranks, qualifiers, and references; its simple
RDF view can omit the latter.

The scoped relation universe is:

1. Inventory **all** IRI-to-IRI predicates used in the frozen WarSampo graph,
   including CIDOC CRM and other external predicates. Record literal-valued
   predicates separately as attributes. This global inventory prevents the
   linked-actor seed set from silently defining the property universe.
2. Find all WarSampo resources with direct Wikidata identity links. Search
   remaining relevant classes (conflict, event, place, unit, person, and
   organization) for verifiable Wikidata identities. Record both directions
   of every WarSampo statement incident to an approved identity.
3. Inventory every Wikidata item-valued statement whose subject **or object**
   is an approved Wikidata endpoint. Keep ordinary, preferred, and deprecated
   ranks as separately marked records, with references and qualifiers. An
   outgoing entity-API response alone does not cover incoming statements.
4. Every predicate pair with at least one verified same-object endpoint pair,
   plus definition-based candidates where no such pair is available. Include
   candidate event paths separately. Do not discard a predicate because its
   frequency is low.

This is **complete relative to the frozen sources and approved identity
crosswalk**, not a claim of complete WWII knowledge. Report the unaligned
events and places as a coverage gap, not as absent relationships.

### Reproducible collection protocol

Use these query shapes; retain actual queries, retrieval times, pagination
keys, result counts, and failed pages in a run manifest. Use source dumps if
an endpoint cannot complete a scan. A timeout does not mean zero results.

```sparql
# WarSampo: global property inventory. Repeat by named graph if the endpoint
# separates them. Use COUNT(*) and the subject/object class distribution.
SELECT ?p (COUNT(*) AS ?n) WHERE {
  ?s ?p ?o . FILTER(isIRI(?s) && isIRI(?o))
} GROUP BY ?p

# WarSampo: all identity links. Normalize Wikidata URL forms to QIDs after
# retrieval, then validate each candidate. Keep non-Wikidata links in audit.
SELECT DISTINCT ?s ?target WHERE {
  ?s <http://www.w3.org/2002/07/owl#sameAs> ?target .
  FILTER(isIRI(?target))
}

# WarSampo: incident edges for one approved source ID.
SELECT ?s ?p ?o WHERE {
  { BIND(<SOURCE_ID> AS ?s) ?s ?p ?o . FILTER(isIRI(?o)) }
  UNION
  { BIND(<SOURCE_ID> AS ?o) ?s ?p ?o . FILTER(isIRI(?s)) }
}
```

For Wikidata, fetch **full statements** for aligned QIDs via the entity API
and extract outgoing item-valued main snaks with their rank, qualifiers,
references, and statement IDs. For incoming edges, query a frozen Wikidata
RDF dump or an indexed SPARQL service for `?s ?wdtPredicate wd:QID`; retain
the statement nodes or fetch each subject's full claim before interpreting
qualifiers and rank. A `wdt:` truthy triple alone omits some statement detail.
Page or partition both source scans deterministically. Compare obtained
counts with a second grouped count; mark any incomplete partition as a
coverage failure. Record `somevalue`, `novalue`, literals, and external-ID
claims outside the item-to-item pair comparison rather than converting them
to a false endpoint.

After every accepted identity bridge, inspect its incident edges and queue
previously unseen endpoint objects for identity review. Repeat until no new
approved identities arise **within the frozen class scope**. Also check
unlinked WarSampo conflict, event, and place classes directly by labels,
dates, types, and sitelinks. Keep every unresolved candidate in the manifest;
do not call the crosswalk exhaustive merely because the queue is empty.

A live read-only inventory on 11 October 2026 found **159** WarSampo resources
with a Wikidata `owl:sameAs` URI: **79 people and 80 military units**. No event,
place, or conflict appeared in that direct-link set. Around those linked
actors, observed outgoing IRI predicates included `hasConflict` (93 triples),
while incoming predicates included CIDOC `P11_had_participant` (5,014),
`P144_joined_with` (4,450), `P143_joined` (137), `P95_has_formed` (255), and
`P138_represents` (5,372). These counts are **source triples**, often from
many different event, image, or record nodes; they are not cross-source
matches or independent evidence. They show why a conflict-only search misses
most candidate predicate families.

## 3. Build the identity crosswalk first

Use three explicit levels. Each row stores the WarSampo URI, Wikidata QID,
object class, identity evidence, reviewer decision, and source date.

| Level | Candidate source | Required decision |
| --- | --- | --- |
| A | WarSampo `owl:sameAs` to a QID | Check names/aliases, type, and temporal scope. A source `sameAs` claim is not automatic approval. |
| B | Conflict and named-event title, Wikipedia sitelink, dates, and known participants/places | Confirm the same historical occurrence and its granularity. A war, battle, and episode within it stay distinct. |
| C | Place or unit aliases, historic name, location, organization stage, and dates | Confirm that the objects are the same place/unit at the relevant time; mark class-versus-instance and headquarters-versus-unit errors. |

Keep `accepted`, `rejected`, and `unresolved` links. Do not use a link under
review in the predicate test. In the POC, WarSampo's single **Sisu** boat
linked to a Wikidata **boat class**, and one air-defense headquarters linked
to a Wikimedia category. Such links can create false predicate matches even
when QIDs and predicate pairs appear to agree. Wikipedia links and text
co-mentions can propose an identity; they are not identity proof by themselves.

The first two conflict bridges to check are WarSampo
[Winter War](http://ldf.fi/warsa/conflicts/WinterWar) ↔ Wikidata
[Q134949](https://www.wikidata.org/wiki/Q134949), and WarSampo
[Continuation War](http://ldf.fi/warsa/conflicts/ContinuationWar) ↔
[Q122100](https://www.wikidata.org/wiki/Q122100). Their English labels and
start/end dates agree. Check Second World War in the same manner before using
it as a parent endpoint.

## 4. Inventory predicate types, not a new fact dataset

Read both the schema and observed graph usage. WarSampo's own schema does not
list every CIDOC CRM predicate used in its data. Inventory both outgoing and
incoming predicates for every aligned endpoint. For each predicate, record:
URI, label, definition/scope note, declared and observed subject/object types,
direction, super/subproperties, inverse, statement count, and one source
example. On the Wikidata side, record the property ID, official description,
constraints, datatype, role/time qualifiers, ranks, and references.

Review every observed predicate family. The following is a **search guide**,
not a preapproved mapping list:

| WarSampo side | Wikidata properties to test | Key question |
| --- | --- | --- |
| `hasConflict`, CIDOC `P11_had_participant`, `P14_carried_out_by` | [`P607`](https://www.wikidata.org/wiki/Property:P607), [`P710`](https://www.wikidata.org/wiki/Property:P710) | Same conflict/event? Same participant role? Same or reversed direction? |
| CIDOC `P10_falls_within`, event `related_period` | [`P361`](https://www.wikidata.org/wiki/Property:P361), [`P527`](https://www.wikidata.org/wiki/Property:P527) | Temporal containment, event part, and “related period” are distinct meanings. |
| CIDOC `P7_took_place_at` | [`P276`](https://www.wikidata.org/wiki/Property:P276), other place properties | Same event and same place scale? A country and a battle site are different endpoints. |
| CIDOC `P143_joined`, `P144_joined_with`, `P95_has_formed`, `P99_dissolved` | [`P463`](https://www.wikidata.org/wiki/Property:P463), [`P112`](https://www.wikidata.org/wiki/Property:P112), organization and date properties | Is the Wikidata statement a direct membership/formation fact, while WarSampo uses an event node? Test a **path rule** separately. |
| CIDOC `P98_brought_into_life`, `P100_was_death_of` | birth/death event and date properties | Do not equate a birth/death event relation with its date literal. |
| CIDOC `P138_represents`, `P70_documents`, article `mentions*` | [`P180`](https://www.wikidata.org/wiki/Property:P180), [`P1343`](https://www.wikidata.org/wiki/Property:P1343), article/media properties | Representation, documentation, subject, and mere mention are not interchangeable. Keep this family in the audit even if the target graph excludes media. |
| `owl:sameAs`, `foaf:page`, `dcterms:source`, `rdf:type`, `hasUnitCategory` | identifiers, sitelinks, references, `P31` | Handle identity, provenance, and classification separately from historical event relations. |

Do not map a property only because two labels resemble each other. Check
official [CIDOC CRM definitions](https://cidoc-crm.org/html/cidoc_crm_v7.1.3.html)
and [Wikidata property pages](https://www.wikidata.org/wiki/Wikidata:List_of_properties).

## 5. Compare statements only after endpoint alignment

Represent a source statement for analysis as
`(source, subject-ID, predicate-ID, object-ID, direction, rank, qualifiers,
valid-time, references, source-record-ID)`. This is a temporary analysis view,
not an exported graph. Use the approved crosswalk to convert both endpoints
to the same canonical object IDs.

For every WarSampo predicate `w` and Wikidata property `d`, count:

- **Same-direction overlap:** `w(a,b)` and `d(a,b)` where both `a` and `b`
  are independently verified as the same real objects.
- **Reverse-direction overlap:** `w(a,b)` and `d(b,a)` under the same condition.
- **Eligible coverage:** the number of `w` pairs and `d` pairs for which
  *both* endpoints have approved identities, whether or not the other source
  makes the same claim.

Report `shared / eligible-WarSampo` and `shared / eligible-Wikidata` with
both denominators. Count unique endpoint pairs and source statements
separately. A high overlap score is a **ranking signal**, not a semantic proof.
If either endpoint is unresolved, put the pair in an identity-work queue; do
not let it vote for a predicate mapping. Inspect rare predicates too: a
single well-defined edge may be useful, even when statistical overlap is zero.

Apply time at the **statement or event** level. Keep Wikidata `P580`/`P582`,
role qualifiers such as `P3831`, and WarSampo event time-span IDs separate
from predicate names. If both sources give intervals, test compatible time
and precision; a missing date is unknown, not a contradiction. A conflict's
overall dates do not prove when a person or unit participated. Preserve
source-specific ranks, references, and WarSampo source URIs. Wikidata
[qualifiers](https://www.wikidata.org/wiki/Help:Qualifiers) can restrict a
claim's meaning.

## 6. Separate direct predicates from path rules

A direct edge can map to a direct edge only after the same endpoints are
verified. If WarSampo models a fact through an intermediate event, compare
the **whole path** to the Wikidata claim and register a path rule with its own
preconditions. Never collapse these cases into a predicate equivalence.

For example, `event P11_had_participant person` plus
`event related_period WinterWar` does **not** prove
`person P607 WinterWar`. The event may be an administrative change during the
war, not the war itself. Likewise, a joining event can support membership
only after its target group, joining actor, and date are checked. Generic
participation cannot establish attacker, defender, or commander roles.

## 7. Decide semantic relation and review every candidate

For each candidate, compare formal definitions and the reviewed shared pairs.
Use these mutually clear decisions:

| Decision | Test |
| --- | --- |
| Equivalent **within scope** | Same direction, compatible types and granularity, and each predicate implies the other under stated conditions. |
| Inverse | Same meaning with endpoints reversed; also record any narrower/broader scope. |
| Narrower / broader | One implication holds, the reverse does not. State the restricting actor, event, role, or time condition. |
| Related | Same pairs appear, but the assertions differ: for example, WarSampo `P10 falls within` versus Wikidata `P361 part of`. |
| Path rule | A direct statement corresponds to a verified multi-edge event path, with explicit conditions. |
| Rejected | Definitions conflict or a supposed shared endpoint is a different object. |
| Unresolved | Identity, definition, temporal scope, or source evidence is insufficient. |

Review **every candidate predicate pair**, including one-off and reverse
matches. For a proposed accepted rule, inspect at least three distinct,
verified supporting pairs if available, across more than one subject; inspect
likely counterexamples and the official definitions. Fewer than three pairs
may still establish a published ontology relation, but empirical support must
then be marked limited. Two reviewers should resolve ambiguous identity or
meaning decisions. No frequency threshold overrides a contradictory
definition. Missing statements are not negative examples.

The current POC's `hasConflict` ↔ `P607` is accepted only for **military unit
→ conflict**. `P710` → CIDOC `P11` is a one-way definition-level candidate:
Wikidata requires active participation, while CIDOC allows active or passive
participation. Wikidata's [P710 page](https://www.wikidata.org/wiki/Property:P710)
also records CIDOC `P11` as an equivalent external property. Record that
published crosswalk as candidate evidence, but preserve the difference in
their written scope until it is resolved for this dataset. WarSampo `P10` and
Wikidata `P361` share Winter and
Continuation War pairs, but temporal containment and part–whole are not
equivalent by default. These decisions and further candidates are listed in
the [POC registry](../poc/relation_alignment/README.md).

## 8. Registry and completeness gate

Publish **one row per mapping decision**, including rejected and unresolved
rows. Minimum fields:

`war_predicate`, `wikidata_property`, `direction`, `mapping_class`,
`subject_type_scope`, `object_type_scope`, `event_granularity_rule`,
`time_and_role_qualifier_rule`, `definition_links`, `source_versions`,
`same_pair_count`, `eligible_pair_denominators`, `reviewed_example_ids`,
`counterexample_ids`, `identity_status`, `provenance`, `reviewer`, `decision_date`.

The audit is complete only when it reports:

1. All observed WarSampo IRI predicates in the frozen scope, including
   incoming edges, each marked `reviewed`, `out of target scope`, or
   `identity blocked`.
2. All eligible Wikidata properties and their use counts, including properties
   with zero matched WarSampo pairs.
3. Counts by identity level and object class: accepted, rejected, unresolved;
   especially the number of conflicts, events, and places still without an
   approved crosswalk.
4. Counts of same-direction and reverse shared pairs, both eligible
   denominators, and reviewed mapping decisions by class.
5. A list of path-rule candidates, semantic counterexamples, missing source
   definitions, and source outages. No accepted mapping should depend on a
   blocked endpoint.

This gate makes “all available overlap” testable. It measures the limits of
the crosswalk as well as the mappings; it does not require exporting the
underlying fact dataset to the project graph.

## 9. Optional EventKG extension

Only after the two-source registry is stable, test whether EventKG adds
previously unaligned named events or new **independent** relation contexts.
Inspect its `owl:sameAs` links, named graphs, direct event properties, and
reified `rdf:subject`/`rdf:object`/`sem:roleType` statements separately.
Retain the EventKG release and source graph. Classify Wikidata-derived
EventKG facts as reused Wikidata evidence. Do not promote `sem:hasActor`
text-event links or Wikipedia co-mentions to participation without a typed
role and verified event identity. Add EventKG only if it reduces a measured
identity or mapping gap; it is not a prerequisite for this process.

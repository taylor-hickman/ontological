"""Model import and export: upstream's native YAML, extended with synonyms and certification, and
Apache Ossie through the `apache-ossie-nvidia-auto-ontology` converter (ARCHITECTURE.md §9.3).

`schemas` is the document, `service` reads and writes it, and the vendored DAL's port,
`auto_ontology.dal.model_interchange`, holds the queries. An import commits in one transaction,
then re-embeds each database it wrote as a tracked `reembed` job (`ontological.embed.reembed`).
"""

# Skill Collection

This context defines the vocabulary for mirroring selected skills from GitHub repositories into one managed collection.

## Language

**Source Repository**:
A public GitHub repository that owns skill content selected for import.
_Avoid_: Upstream repo, remote repo

**Source Path**:
A directory inside a Source Repository that groups one or more skills available for selection.
_Avoid_: Parent folder, skills path

**Source Skill**:
A skill inside a Source Path that has been explicitly selected for import.
_Avoid_: Source directory, upstream skill, `skill_dir`

**Managed Skill**:
The local mirror of exactly one Source Skill, identified by a globally unique destination name. A Source Skill may have only one Managed Skill.
_Avoid_: Local skill, target skill, `skill_dir`

**Skill Mapping**:
The one-to-one association between a Source Skill and its Managed Skill.
_Avoid_: Alias, duplicate import

**Manifest**:
The authoritative declaration of every Managed Skill and the Source Skill from which it is mirrored. Human comments in the Manifest carry no system meaning.
_Avoid_: Skills list, update config

**Lock Record**:
The persisted provenance and content identity of a Managed Skill from its most recently applied Sync Plan.
_Avoid_: Cache entry, history record

**Sync Plan**:
An immutable, fully validated set of Sync Operations calculated from one snapshot of all configured sources.
_Avoid_: Dry run, pending changes

**Sync Operation**:
One addition, update, or deletion of a Managed Skill described by a Sync Plan.
_Avoid_: File change, sync task

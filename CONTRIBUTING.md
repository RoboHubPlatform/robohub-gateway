# Contributing to robohub-gateway

robohub-gateway is part of RoboHub. Changes are welcome as pull requests.

## How a change is made

Every change starts from a requirement and ends with a test that proves it,
with the decisions that shaped it written down between the two. The process,
its levels and its tools are described once, for the whole ecosystem, in
[How a change is made](https://github.com/RoboHubPlatform/robohub/blob/main/docs/doc/process/index.md).
In short:

1. **Requirement.** Name the requirement your change serves, or propose one in
   `docs/requirements/` with `status: proposed` (`rho req new` writes it).
2. **Decision.** If the change alters how the system is built, propose an ADR in
   `docs/architecture/adr/` (`rho req adr new`).
3. **Code and tests.** Tag the code that meets a requirement `// @req <ID>` and
   the tests that prove it `// @verifies <ID>`.
4. **Check.** `rho req check` must pass; `rho req trace` shows what is covered.

Fill in the pull request template: it asks for exactly these.

## Building and testing

The README says how to build and test this repository. CI runs the shared
RoboHub workflow on every push and pull request; a pull request is merged only
with it green.

## Who decides

Anyone may propose a requirement, an ADR or code. The maintainer, named in
[MAINTAINERS.yml](https://github.com/RoboHubPlatform/robohub/blob/main/MAINTAINERS.yml),
approves requirements, accepts ADRs and merges to `main`. `main` is protected,
and CODEOWNERS requires the maintainer's review for requirements, architecture
decisions and `module.yml`. `rho req check` also refuses an approval not made by
the maintainer — a guard on top, since a commit's author can be written by
anyone; the protected branch is what makes it hold.

## Licence

Contributions are made under this repository's licence (see `LICENSE`).

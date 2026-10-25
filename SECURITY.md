# Security policy

## Supported versions

Security fixes target the latest released minor series, currently **0.2.x**.
Earlier series are not maintained for security fixes. Upgrade to the latest
patch release before reporting a problem when practical. Fixes are provided
on a best-effort basis; there is no guaranteed response or resolution time.

## Report a vulnerability privately

Email [zheltov_pe@mail.ru](mailto:zheltov_pe@mail.ru). Do not open a public issue
or include an exploit in a public pull request before coordinated disclosure.

Include the affected version, Python version, impact, reproduction steps and
a minimal example using synthetic data. Relevant areas include credential or
cursor leakage, requests sent to disallowed continuation targets, unsafe
command execution and trace/replay validation. Do not send live credentials
or production datasets. Ask for a suitable transfer method before sending
material that cannot safely be shared by ordinary email.

The maintainer will investigate and coordinate a fix and disclosure with the
reporter. A report should describe a security impact; ordinary pagination
findings and feature requests belong in
[public issues](https://github.com/0then0/cursorproof/issues).

## Safe operation

- Treat configuration files as trusted input. Oracle commands, mutation hooks
  and boundary fixtures execute configured programs and can change data.
- Use read-only credentials where possible and controlled test environments
  for commands and mutation checks. Only test APIs you are authorized to access.
- Review traces before sharing them. Redaction helps protect known secrets
  but cannot identify every sensitive value or dataset identifier.
- Offline replay parses recorded observations without contacting the API;
  live replay makes requests and can execute configured commands. Review its
  configuration and use the same precautions as for a new run.

See the [user guide](docs/guide.md#reproduction-and-privacy) for trace privacy
and replay behavior. A successful pagination check is not a security audit of
the tested API.

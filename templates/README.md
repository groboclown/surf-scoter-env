# Configuration templates

`scoter init [NAME]` and `scoter unlock --from-template NAME` start a
configuration from one of these directories, copied into your unlocked
directory.  A template is never run as it is: `scoter lock` reviews it like
any other configuration before it's stored.

The setup's `pull: paths: templates-dir` names this directory, and
`pull: values: default-template` the template `scoter init` uses by default.

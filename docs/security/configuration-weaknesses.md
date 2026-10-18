# Weak Points in the Configuration

At some level, a user may need the Surf Scoter dev environment to access things outside the environment in order to have the developer do productive work.  This provides a major point of weakness, and this document examines how the user settings and project configuration can lead to unintentional leaks or attacks.  The end goal of this analysis leads into constructing tools to analyze the configuration to help developers make smart decisions.


# Resisting Weaknesses

In order to combat these weaknesses, the tool provides some capabilities to help the users make informed decisions, and to know when potentially unexpected events happened.

## Authorization for a Project

Project configuration files provide the biggest weakpoint, as the user can pull unknown configurations and unintentionally run them.  This can happen on first download, or on a pull from a shared repository.

The tool requires authorization for a project by:

- Record in the user's scotor directory authorized projects, along with checksums of the authorized project configuration.
    - A user may perform additional authorization by either permanently or for a time period disable the checksum analysis.  This can be useful when the user actively works on making a running environment.
    - Some speed advantages can be made for things like, "allow local changes but scan upstream changes," which requires knowledge of VCS usage.
    - A limited history may provide use - someone who's switching between branches shouldn't need to re-authorize on every switch.  However, just because something historically was fine doesn't mean it continues to be fine; for example, the accessed cloud environment may change its security requirements, which means old branches have more permissions than current branches should have.
- Before `run` steps, check the configuration against the authorized list.  Changes trigger the re-authorization.
- If the tool itself contains malware checks, updates to those checks may trigger new processing.  In these cases, the new processing should only require authorization (or new blocking) on positives.

Along with authorization, the tool must provide easy mechanisms to revoke authorization.

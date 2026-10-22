# Contributors' Handbook

*This acts to fill in the details summarized in the [main contributor guide](../../CONTRIBUTORS.md).*

* [Code Style Guide](code-style.md)
* [V0 Architecture](architecture-v0.md)
* [Future Ideas](future-ideas.md)
* [Towards V1](towards-v1.md)
* [Projects in V1](projects-v1.md)
* [The universal configuration file](universal-configuration-file.md)
* [Creating and maintaining sub-commands](sub-commands.md)


## A Note about Performance

The primary execution command, `scoter run`, is expected to remain running for an extended period of time, and the user runs it infrequently.  While performance is important, shaving off 3 seconds in one place doesn't add that much improvement.  Security and readability take the front seat to performance.

But a 3 second shave looped 30 times?  That's something worth considering.

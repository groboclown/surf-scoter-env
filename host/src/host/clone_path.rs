//! Files in the project clone, reached without following anything the
//! container put there (BRIDGE-3).
//!
//! The container can change the clone at any moment: replace a directory
//! with a symlink, say, between a check and a use, or while a host program
//! runs.  So a path into the clone is never handed to a host program.
//! Instead, each file is reached from the clone's root one name at a time,
//! never following a symlink (`openat` with `O_NOFOLLOW` on Unix), and:
//!
//! * a file the program reads is copied, through the open file, into the
//!   request's private temporary directory, under its own name;
//! * a file the program writes is written there, then put back into the
//!   clone through the open directory, as a new file renamed over the old
//!   (a rename replaces a symlink at the name; it doesn't follow it);
//! * the working directory is opened the same way, and the process starts
//!   in that open directory (`fchdir`).
//!
//! The clone's root itself is a host directory the container can't reach
//! (`project-state: paths: project-clone-dir`, FILE-10), so it's opened by
//! path.  Names are plain: never empty, `.`, `..`, or with a `/` in them.
//!
//! On Windows, which has no `openat`, each name is checked not to be a
//! symlink or junction, then used by path: the copying still keeps the
//! program away from the clone, but the copy itself can race.

use std::fs::{self, File};
use std::io::{self, ErrorKind};
use std::path::{Path, PathBuf};

/// What's at a path in the clone.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Kind {
    File,
    Dir,
    /// A symlink, a FIFO, a socket or a device: never used.
    Other,
    /// Nothing yet; its directory exists.
    Missing,
}

/// Whether `name` is a plain name for one directory entry.
pub(crate) fn is_plain_name(name: &str) -> bool {
    !name.is_empty() && name != "." && name != ".." && !name.contains('/') && !name.contains('\0')
}

fn refuse(message: &str) -> io::Error {
    io::Error::new(ErrorKind::InvalidInput, message.to_string())
}

fn check_names(names: &[String]) -> io::Result<()> {
    if names.iter().all(|n| is_plain_name(n)) {
        Ok(())
    } else {
        Err(refuse("not a plain name"))
    }
}

/// The path, for messages: `root` with `names` added.
pub(crate) fn display_path(root: &Path, names: &[String]) -> PathBuf {
    names
        .iter()
        .fold(root.to_path_buf(), |path, name| path.join(name))
}

#[cfg(unix)]
mod imp {
    use super::{Kind, check_names, refuse};
    use std::ffi::CString;
    use std::fs::File;
    use std::io::{self, Read, Write};
    use std::os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd};
    use std::os::unix::ffi::OsStrExt;
    use std::path::Path;
    use std::sync::atomic::{AtomicUsize, Ordering};

    fn c_name(name: &str) -> io::Result<CString> {
        CString::new(name).map_err(|_| refuse("a name with a NUL"))
    }

    fn open_at(
        dir: RawFd,
        name: &str,
        flags: libc::c_int,
        mode: libc::mode_t,
    ) -> io::Result<OwnedFd> {
        let c = c_name(name)?;
        // SAFETY: `c` is a valid C string; the result is checked.
        let fd = unsafe {
            libc::openat(
                dir,
                c.as_ptr(),
                flags | libc::O_CLOEXEC,
                libc::c_uint::from(mode),
            )
        };
        if fd < 0 {
            return Err(io::Error::last_os_error());
        }
        // SAFETY: a new descriptor only this owns.
        Ok(unsafe { OwnedFd::from_raw_fd(fd) })
    }

    fn open_root(root: &Path) -> io::Result<OwnedFd> {
        let c =
            CString::new(root.as_os_str().as_bytes()).map_err(|_| refuse("a path with a NUL"))?;
        // SAFETY: `c` is a valid C string; the result is checked.
        let fd = unsafe {
            libc::open(
                c.as_ptr(),
                libc::O_RDONLY | libc::O_DIRECTORY | libc::O_CLOEXEC,
            )
        };
        if fd < 0 {
            return Err(io::Error::last_os_error());
        }
        // SAFETY: a new descriptor only this owns.
        Ok(unsafe { OwnedFd::from_raw_fd(fd) })
    }

    /// The directory `names` below `root`, open; no name may be a symlink.
    pub(crate) fn open_dir(root: &Path, names: &[String]) -> io::Result<OwnedFd> {
        check_names(names)?;
        let mut dir = open_root(root)?;
        for name in names {
            dir = open_at(
                dir.as_raw_fd(),
                name,
                libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW,
                0,
            )?;
        }
        Ok(dir)
    }

    fn stat_at(dir: RawFd, name: &str) -> io::Result<libc::stat> {
        let c = c_name(name)?;
        // SAFETY: zeroed is a valid `stat`; the call fills it, or fails.
        let mut st: libc::stat = unsafe { std::mem::zeroed() };
        // SAFETY: valid C string, valid out pointer; the result is checked.
        if unsafe { libc::fstatat(dir, c.as_ptr(), &raw mut st, libc::AT_SYMLINK_NOFOLLOW) } != 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(st)
    }

    fn kind_of(mode: libc::mode_t) -> Kind {
        match mode & libc::S_IFMT {
            libc::S_IFREG => Kind::File,
            libc::S_IFDIR => Kind::Dir,
            _ => Kind::Other,
        }
    }

    /// What's at `names` below `root`, without following a symlink anywhere.
    pub(crate) fn kind(root: &Path, names: &[String]) -> io::Result<Kind> {
        check_names(names)?;
        let Some((last, parents)) = names.split_last() else {
            return open_dir(root, names).map(|_| Kind::Dir);
        };
        let dir = open_dir(root, parents)?;
        match stat_at(dir.as_raw_fd(), last) {
            Ok(st) => Ok(kind_of(st.st_mode)),
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(Kind::Missing),
            Err(e) => Err(e),
        }
    }

    /// The regular file `names` below `root`, open for reading, and its permission bits.
    pub(crate) fn open_file(root: &Path, names: &[String]) -> io::Result<(File, u32)> {
        check_names(names)?;
        let (last, parents) = names.split_last().ok_or_else(|| refuse("no file name"))?;
        let dir = open_dir(root, parents)?;
        // `O_NONBLOCK`, so a FIFO put there can't hang the open.
        let fd = open_at(
            dir.as_raw_fd(),
            last,
            libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK,
            0,
        )?;
        let file = File::from(fd);
        let meta = file.metadata()?;
        if !meta.file_type().is_file() {
            return Err(refuse("not a regular file"));
        }
        use std::os::unix::fs::PermissionsExt;
        Ok((file, meta.permissions().mode() & 0o777))
    }

    /// Put the contents of `source` at `names` below `root`, as a new file
    /// renamed over whatever is there: a symlink at the name is replaced,
    /// never followed.  The new file gets `mode`.
    pub(crate) fn write_back(
        root: &Path,
        names: &[String],
        source: &mut File,
        mode: u32,
    ) -> io::Result<()> {
        static COUNTER: AtomicUsize = AtomicUsize::new(0);
        check_names(names)?;
        let (last, parents) = names.split_last().ok_or_else(|| refuse("no file name"))?;
        let dir = open_dir(root, parents)?;
        let temp = format!(
            ".{last}.scoter-{}-{}",
            std::process::id(),
            COUNTER.fetch_add(1, Ordering::Relaxed),
        );
        let fd = open_at(
            dir.as_raw_fd(),
            &temp,
            libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_NOFOLLOW,
            (mode & 0o777) as libc::mode_t,
        )?;
        let mut out = File::from(fd);
        let renamed = (|| {
            let mut buf = Vec::new();
            source.read_to_end(&mut buf)?;
            out.write_all(&buf)?;
            out.sync_all()?;
            let (from, to) = (c_name(&temp)?, c_name(last)?);
            // SAFETY: valid C strings and descriptors; the result is checked.
            if unsafe {
                libc::renameat(dir.as_raw_fd(), from.as_ptr(), dir.as_raw_fd(), to.as_ptr())
            } != 0
            {
                return Err(io::Error::last_os_error());
            }
            Ok(())
        })();
        if renamed.is_err()
            && let Ok(c) = c_name(&temp)
        {
            // SAFETY: valid C string and descriptor.
            unsafe { libc::unlinkat(dir.as_raw_fd(), c.as_ptr(), 0) };
        }
        renamed
    }
}

#[cfg(not(unix))]
mod imp {
    use super::{Kind, check_names, display_path, refuse};
    use std::fs::{self, File};
    use std::io::{self, Read, Write};
    use std::path::{Path, PathBuf};

    /// `names` below `root`, each checked not to be a symlink or junction.
    /// It's then used by path, so this can race (see the module's notes).
    fn walk(root: &Path, names: &[String]) -> io::Result<PathBuf> {
        check_names(names)?;
        let mut path = root.to_path_buf();
        for name in names {
            path.push(name);
            let meta = fs::symlink_metadata(&path)?;
            if !meta.is_dir() || meta.file_type().is_symlink() {
                return Err(refuse("not a directory"));
            }
        }
        Ok(path)
    }

    pub(crate) fn kind(root: &Path, names: &[String]) -> io::Result<Kind> {
        let Some((last, parents)) = names.split_last() else {
            return walk(root, names).map(|_| Kind::Dir);
        };
        let path = walk(root, parents)?.join(last);
        match fs::symlink_metadata(&path) {
            Ok(m) if m.file_type().is_symlink() => Ok(Kind::Other),
            Ok(m) if m.is_file() => Ok(Kind::File),
            Ok(m) if m.is_dir() => Ok(Kind::Dir),
            Ok(_) => Ok(Kind::Other),
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(Kind::Missing),
            Err(e) => Err(e),
        }
    }

    pub(crate) fn dir_path(root: &Path, names: &[String]) -> io::Result<PathBuf> {
        walk(root, names)
    }

    pub(crate) fn open_file(root: &Path, names: &[String]) -> io::Result<(File, u32)> {
        if kind(root, names)? != Kind::File {
            return Err(refuse("not a regular file"));
        }
        Ok((File::open(display_path(root, names))?, 0o644))
    }

    pub(crate) fn write_back(
        root: &Path,
        names: &[String],
        source: &mut File,
        _mode: u32,
    ) -> io::Result<()> {
        let (last, parents) = names.split_last().ok_or_else(|| refuse("no file name"))?;
        let dir = walk(root, parents)?;
        let temp = dir.join(format!(".{last}.scoter-{}", std::process::id()));
        let mut buf = Vec::new();
        source.read_to_end(&mut buf)?;
        let result = (|| {
            let mut out = fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&temp)?;
            out.write_all(&buf)?;
            out.sync_all()?;
            fs::rename(&temp, dir.join(last))
        })();
        if result.is_err() {
            let _ = fs::remove_file(&temp);
        }
        result
    }
}

#[cfg(not(unix))]
pub(crate) use imp::dir_path;
#[cfg(unix)]
pub(crate) use imp::open_dir;
pub(crate) use imp::{kind, open_file, write_back};

/// Copy the regular file `names` below `root` to `dest` (a new file in a
/// private directory), keeping its permission bits but setuid and the like.
pub(crate) fn copy_in(root: &Path, names: &[String], dest: &Path) -> io::Result<()> {
    let (mut file, mode) = open_file(root, names)?;
    let mut out = File::create(dest)?;
    io::copy(&mut file, &mut out)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        out.set_permissions(fs::Permissions::from_mode(mode & 0o777))?;
    }
    #[cfg(not(unix))]
    let _ = mode;
    Ok(())
}

/// Put `source`, a regular file in a private directory, at `names` below
/// `root` (see `write_back`).  It keeps `source`'s permission bits.
pub(crate) fn copy_out(source: &Path, root: &Path, names: &[String]) -> io::Result<()> {
    let meta = fs::symlink_metadata(source)?;
    if !meta.is_file() {
        return Err(refuse("the program left something other than a file"));
    }
    #[cfg(unix)]
    let mode = {
        use std::os::unix::fs::PermissionsExt;
        meta.permissions().mode() & 0o777
    };
    #[cfg(not(unix))]
    let mode = 0o644;
    let mut file = File::open(source)?;
    write_back(root, names, &mut file, mode)
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::os::unix::fs::symlink;

    struct Root(PathBuf);
    impl Root {
        fn new() -> Self {
            let dir = crate::host::exec::TempDir::new().unwrap();
            let path = dir.path().to_path_buf();
            std::mem::forget(dir);
            fs::create_dir_all(path.join("clone/a/b")).unwrap();
            fs::write(path.join("clone/a/b/f.txt"), "inside").unwrap();
            fs::create_dir_all(path.join("outside")).unwrap();
            fs::write(path.join("outside/secret"), "secret").unwrap();
            Root(path)
        }
        fn clone(&self) -> PathBuf {
            self.0.join("clone")
        }
    }
    impl Drop for Root {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }
    fn names(path: &str) -> Vec<String> {
        path.split('/').map(str::to_string).collect()
    }

    #[test]
    fn kinds() {
        let r = Root::new();
        assert_eq!(kind(&r.clone(), &names("a/b/f.txt")).unwrap(), Kind::File);
        assert_eq!(kind(&r.clone(), &names("a/b")).unwrap(), Kind::Dir);
        assert_eq!(kind(&r.clone(), &[]).unwrap(), Kind::Dir);
        assert_eq!(kind(&r.clone(), &names("a/b/new")).unwrap(), Kind::Missing);
        assert!(kind(&r.clone(), &names("a/missing/new")).is_err());
        symlink(r.0.join("outside"), r.clone().join("a/link")).unwrap();
        assert_eq!(kind(&r.clone(), &names("a/link")).unwrap(), Kind::Other);
        assert!(kind(&r.clone(), &names("a/link/secret")).is_err());
        for bad in ["..", ".", ""] {
            assert!(kind(&r.clone(), &[bad.to_string()]).is_err(), "{bad}");
        }
    }

    #[test]
    fn copy_never_follows_a_symlink() {
        let r = Root::new();
        let dest = r.0.join("copy");
        copy_in(&r.clone(), &names("a/b/f.txt"), &dest).unwrap();
        assert_eq!(fs::read_to_string(&dest).unwrap(), "inside");
        // A file, or a directory on the way, swapped for a symlink out.
        symlink(r.0.join("outside/secret"), r.clone().join("a/b/s")).unwrap();
        assert!(copy_in(&r.clone(), &names("a/b/s"), &r.0.join("copy2")).is_err());
        fs::rename(r.clone().join("a/b"), r.0.join("moved")).unwrap();
        symlink(r.0.join("outside"), r.clone().join("a/b")).unwrap();
        assert!(copy_in(&r.clone(), &names("a/b/secret"), &r.0.join("copy3")).is_err());
    }

    #[test]
    fn write_back_replaces_a_symlink_rather_than_following_it() {
        let r = Root::new();
        let source = r.0.join("out");
        fs::write(&source, "new").unwrap();
        // The container put a symlink where the output goes.
        symlink(r.0.join("outside/secret"), r.clone().join("a/b/out.txt")).unwrap();
        copy_out(&source, &r.clone(), &names("a/b/out.txt")).unwrap();
        assert_eq!(
            fs::read_to_string(r.0.join("outside/secret")).unwrap(),
            "secret"
        );
        let written = r.clone().join("a/b/out.txt");
        assert!(
            !fs::symlink_metadata(&written)
                .unwrap()
                .file_type()
                .is_symlink()
        );
        assert_eq!(fs::read_to_string(&written).unwrap(), "new");
        // A directory on the way swapped for a symlink: refused, nothing written outside.
        fs::rename(r.clone().join("a/b"), r.0.join("moved")).unwrap();
        symlink(r.0.join("outside"), r.clone().join("a/b")).unwrap();
        assert!(copy_out(&source, &r.clone(), &names("a/b/x")).is_err());
        assert!(!r.0.join("outside/x").exists());
        // No temporary file is left behind.
        assert_eq!(fs::read_dir(r.0.join("moved")).unwrap().count(), 2);
    }
}

//! One connection to the service, whatever its endpoint (C1): a Unix socket, a Windows named pipe, or loopback TCP.
//! A connection is cloned into a reading and a writing half that work from two threads at once.

use crate::home::Endpoint;
use std::io::{self, Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::time::Duration;

#[cfg(windows)]
use std::sync::Arc;

pub enum Conn {
    #[cfg(unix)]
    Unix(std::os::unix::net::UnixStream),
    Tcp(TcpStream),
    #[cfg(windows)]
    Pipe(Arc<crate::sys::Pipe>),
}

fn unsupported(what: &str) -> io::Error {
    io::Error::new(io::ErrorKind::Unsupported, format!("{what} endpoints are not used on this system"))
}

impl Conn {
    pub fn connect(endpoint: &Endpoint, timeout: Duration) -> io::Result<Conn> {
        match endpoint {
            #[cfg(unix)]
            Endpoint::Unix(path) => Ok(Conn::Unix(std::os::unix::net::UnixStream::connect(path)?)),
            #[cfg(not(unix))]
            Endpoint::Unix(_) => Err(unsupported("Unix socket")),
            #[cfg(windows)]
            Endpoint::Pipe(name) => Ok(Conn::Pipe(Arc::new(crate::sys::Pipe::connect(name, timeout)?))),
            #[cfg(not(windows))]
            Endpoint::Pipe(_) => Err(unsupported("named pipe")),
            Endpoint::Tcp(address, port) => {
                if address != "127.0.0.1" {
                    return Err(io::Error::new(io::ErrorKind::InvalidData, "a service endpoint off this computer"));
                }
                let target = (address.as_str(), *port).to_socket_addrs()?.next().ok_or_else(|| unsupported("TCP"))?;
                let stream = TcpStream::connect_timeout(&target, timeout)?;
                let _ = stream.set_nodelay(true);
                Ok(Conn::Tcp(stream))
            }
        }
    }

    pub fn try_clone(&self) -> io::Result<Conn> {
        match self {
            #[cfg(unix)]
            Conn::Unix(s) => Ok(Conn::Unix(s.try_clone()?)),
            Conn::Tcp(s) => Ok(Conn::Tcp(s.try_clone()?)),
            #[cfg(windows)]
            Conn::Pipe(p) => Ok(Conn::Pipe(Arc::clone(p))),
        }
    }

    pub fn set_read_timeout(&self, timeout: Option<Duration>) -> io::Result<()> {
        match self {
            #[cfg(unix)]
            Conn::Unix(s) => s.set_read_timeout(timeout),
            Conn::Tcp(s) => s.set_read_timeout(timeout),
            #[cfg(windows)]
            Conn::Pipe(p) => {
                p.set_read_timeout(timeout);
                Ok(())
            }
        }
    }

    /// Ends the connection for both halves (a thread reading it stops).
    pub fn shutdown(&self) {
        match self {
            #[cfg(unix)]
            Conn::Unix(s) => {
                let _ = s.shutdown(std::net::Shutdown::Both);
            }
            Conn::Tcp(s) => {
                let _ = s.shutdown(std::net::Shutdown::Both);
            }
            #[cfg(windows)]
            Conn::Pipe(p) => p.cancel_all(),
        }
    }
}

impl Read for Conn {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        match self {
            #[cfg(unix)]
            Conn::Unix(s) => s.read(buf),
            Conn::Tcp(s) => s.read(buf),
            #[cfg(windows)]
            Conn::Pipe(p) => (&**p).read(buf),
        }
    }
}

impl Write for Conn {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        match self {
            #[cfg(unix)]
            Conn::Unix(s) => s.write(buf),
            Conn::Tcp(s) => s.write(buf),
            #[cfg(windows)]
            Conn::Pipe(p) => (&**p).write(buf),
        }
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

import logging
import ipaddress
from urllib.parse import urlparse, urlunparse

from .helpers import (
    make_event_id,
    is_event_id,
    get_event_type,
)
from bbot.core.errors import *
from bbot.core.helpers import (
    extract_words,
    tldextract,
    split_host_port,
    host_in_host,
    is_domain,
    is_subdomain,
)


log = logging.getLogger("bbot.core.event")


class BaseEvent:

    _dummy = False
    _internal_only = False

    def __init__(
        self,
        data,
        event_type=None,
        source=None,
        module=None,
        scan_id=None,
        tags=None,
        confidence=100,
        _dummy=False,
    ):

        # for creating one-off events without enforcing source requirement
        self._dummy = _dummy

        if tags is None:
            tags = set()

        self.module = module
        self.scan_id = scan_id

        self.source = None
        if BaseEvent in source.__class__.__bases__:
            self.source = source.id
        elif is_event_id(source):
            self.source = str(source)
        if (not self.source) and (not self._dummy):
            raise ValidationError(f"Must specify event source")

        # if the event is internal, erase it from the chain of events
        if self._internal_only:
            self._id = self.source

        self.type = event_type
        self.tags = set(tags)
        self.confidence = int(confidence)
        self.data = self._sanitize_data(data)

        if not self.data:
            raise ValidationError(f'Invalid event data "{data}" for type "{self.type}"')

        self._id = None
        self._hash = None
        self.__host = None
        self._port = None
        self.__words = None

    @property
    def host(self):
        """
        An abbreviated representation of the data that allows comparison with other events.
        For host types, this is a hostname.
        This allows comparison of an email or a URL with a domain, and vice versa
            bob@evilcorp.com --> evilcorp.com
            https://evilcorp.com --> evilcorp.com

        For IPV*_* types, this is an instantiated object representing the event's data
        E.g. for IP_ADDRESS, it's an ipaddress.IPv4Address() object
        """
        if self.__host is None:
            self.__host = self._host()
        return self.__host

    @property
    def port(self):
        self.host
        return self._port

    @property
    def host_stem(self):
        """
        An abbreviated representation of hostname that removes the TLD
            E.g. www.evilcorp.com --> www.evilcorp
        """
        if self.host and type(self.host) == str:
            parsed = tldextract(self.data)
            return f".".join(parsed.subdomain.split(".") + parsed.domain.split(".")).strip(".")
        else:
            return f"{self.host}"

    @property
    def words(self):
        if self.__words is None:
            self.__words = set(self._words())
        return self.__words

    def _words(self):
        return set()

    @property
    def data_hash(self):
        if self._hash is None:
            self._hash = sha1(self.data)
        return self._hash

    @property
    def id(self):
        if self._id is None:
            self._id = make_event_id(self.data, self.type)
        return self._id

    def _sanitize_data(self, data):
        return data

    def __contains__(self, other):
        """
        Allows events to be compared using the "in" operator:
        E.g.:
            if some_event in other_event:
                ...
        """
        try:
            other = make_event(other, dummy=True)
        except ValidationError:
            return False
        # if hashes match
        if other == self:
            return True
        # if hosts match
        if self.host and other.host:
            if self.host == other.host:
                return True
            # hostnames and IPs
            return host_in_host(other.host, self.host)
        return False

    @property
    def json(self):
        j = dict()
        for i in ("type", "data", "source", "id"):
            v = getattr(self, i, "")
            if v:
                j.update({i: v})
        if self.tags:
            j.update({"tags": list(self.tags)})
        if self.module:
            j.update({"module": str(self.module)})
        if self.scan_id:
            j.update({"scan_id": str(self.scan_id)})
        return j

    def __iter__(self):
        yield from self.json.items()

    def __eq__(self, other):
        return hash(self) == hash(other)

    def __hash__(self):
        return hash(self.id)

    def __str__(self):
        d = str(self.data)
        return f'Event("{self.type}", "{d[:50]}{("..." if len(d) > 50 else "")}")'


class DefaultEvent(BaseEvent):
    def _sanitize_data(self, data):
        return data

    def _host(self):
        return ""


class IPAddressEvent(BaseEvent):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        ip = ipaddress.ip_address(self.data)
        self.tags.add(f"ipv{ip.version}")

    def _sanitize_data(self, data):
        return str(ipaddress.ip_address(str(data)))

    def _host(self):
        return ipaddress.ip_address(self.data)


class IPRangeEvent(BaseEvent):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        net = ipaddress.ip_network(self.data, strict=False)
        self.tags.add(f"ipv{net.version}")

    def _sanitize_data(self, data):
        return str(ipaddress.ip_network(str(data), strict=False))

    def _host(self):
        return ipaddress.ip_network(self.data)


class DNSNameEvent(BaseEvent):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if is_subdomain(self.data):
            self.tags.add("subdomain")
        elif is_domain(self.data):
            self.tags.add("domain")

    def _sanitize_data(self, data):
        return str(data).strip().lower()

    def _host(self):
        return self.data

    def _words(self):
        if not "wildcard" in self.tags:
            return extract_words(self.host_stem)
        return set()


class OpenTCPPortEvent(BaseEvent):
    def _sanitize_data(self, data):
        parsed = urlparse(f"d://{data}")
        return parsed.netloc

    def _host(self):
        host, self._port = split_host_port(self.data)
        return host

    def _words(self):
        return extract_words(self.host_stem)


class URLEvent(BaseEvent):
    def _sanitize_data(self, data):
        parsed = urlparse(data.strip())
        parsed = parsed._replace(netloc=str(parsed.netloc).lower())
        return urlunparse(parsed)

    def _host(self):
        host, self._port = split_host_port(self.data)
        return host

    def _words(self):
        return extract_words(self.host_stem)


class EmailAddressEvent(BaseEvent):
    def _host(self):
        data = str(self.data).split("@")[-1]
        host, self._port = split_host_port(data)
        return host

    def _words(self):
        return extract_words(self.host_stem)


class HTTPResponseEvent(BaseEvent):
    _internal_only = True


event_classes = {
    "IP_ADDRESS": IPAddressEvent,
    "IP_RANGE": IPRangeEvent,
    "DNS_NAME": DNSNameEvent,
    "OPEN_TCP_PORT": OpenTCPPortEvent,
    "URL": URLEvent,
    "EMAIL_ADDRESS": EmailAddressEvent,
    "HTTP_RESPONSE": HTTPResponseEvent,
}


def make_event(
    data,
    event_type=None,
    source=None,
    module=None,
    scan_id=None,
    tags=None,
    confidence=100,
    dummy=False,
):
    """
    If data is already an event, simply return it
    """

    if BaseEvent in data.__class__.__bases__:
        if scan_id is not None and not data.scan_id:
            data.scan_id = scan_id
        if module is not None:
            data.module = module
        return data
    else:
        if event_type is None:
            event_type = get_event_type(data)
            if not dummy:
                log.debug(f'Autodetected event type "{event_type}" based on data: "{data}"')
        if event_type is None:
            raise ValidationError(f'Unable to autodetect event type from "{data}": please specify event_type')

        event_type = str(event_type).strip().upper()
        event_class = event_classes.get(event_type, DefaultEvent)

        return event_class(
            data,
            event_type=event_type,
            source=source,
            module=module,
            scan_id=scan_id,
            tags=tags,
            confidence=confidence,
            _dummy=dummy,
        )
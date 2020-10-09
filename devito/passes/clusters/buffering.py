from collections import OrderedDict

from cached_property import cached_property

from devito.ir.equations import DummyEq
from devito.ir.clusters import Queue
from devito.ir.support import SEQUENTIAL, Scope
from devito.tools import DefaultOrderedDict, flatten, timed_pass
from devito.types import Array, CustomDimension, ModuloDimension

__all__ = ['Buffering']


class Buffering(Queue):

    """
    Replace Functions matching a user-provided condition with Arrays. The
    computation is then performed over such Arrays, while the buffered
    Functions are only accessed for initialization and finalization.

    The read-only Functions are not buffering candidates.

    Parameters
    ----------
    key : callable, optional
        Apply buffering iff `key(function)` gives True.

    Examples
    --------
    If we have a Cluster with the following Eq

        Eq(u[time+1, x], u[time, x] + u[time-1, x] + 1)

    Then we see that `u(time, x)` is both read and written. So it is a buffering
    candidate. Let's assume that `key(u)` is True, so we apply buffering. This
    boils down to:

        1. Introduce one Cluster with two Eqs to initialize the buffer, i.e.

            Eq(u_buf[d, x], u[d, x])
            Eq(u_buf[d-1, x], u[d-1, x])

           With the ModuloDimension `d` (a sub-iterator along `time`) starting at
           either `time.symbolic_min` (Forward direction) or `time.symbolic_max`
           (Backward direction).

        2. Introduce one Cluster with one Eq to dump the buffer back into `u`

            Eq(u[time+1, x], u_buf[d+1, x])

        3. Replace all other occurrences of `u` with `u_buf`

    So eventually we have three Clusters:

        Cluster([Eq(u_buf[d, x], u[d, x]),
                 Eq(u_buf[d-1, x], u[d-1, x])])
        Cluster([Eq(u_buf[d+1, x], u[d, x] + u[d-1, x] + 1)])
        Cluster([Eq(u[time+1, x], u_buf[d+1, x])])
    """

    def __init__(self, key=None):
        if key is None:
            self.key = lambda f: f.is_DiscreteFunction
        else:
            assert callable(key)
            self.key = lambda f: f.is_DiscreteFunction and key(f)

        super(Buffering, self).__init__()

    @timed_pass(name='buffering')
    def process(self, clusters):
        super().process(clusters)

    def callback(self, clusters, prefix):
        if not prefix:
            return clusters

        d = prefix[-1].dim

        if not all(SEQUENTIAL in c.properties[d] for c in clusters):
            return clusters

        # Map buffered Functions to their reading and last-writing Cluster
        #TODO: rollback to wmapper and rmapper
        #TODO: then create mapper[f] = Buffer(f, d, wmapper[f], rmapper[f])
        #TODO: then in the constructor of Buffer do create the actual Buffer
        #TODO: this will make Buffer immutable (at last!!!)
        #TODO: will also compute slots and create mds. Will have a mapper between
        #TODO: slots (eg time+1) and mds (eg d0 representing (time+1)%2)
        mapper = BufferMapper(d)
        for c in clusters:
            for f in c.scope.writes:
                if self.key(f):
                    mapper[f].lastwrite = c
            for f in c.scope.reads:
                if self.key(f):
                    mapper[f].readby.append(c)

        # Create buffers
        for f, b in mapper.items():
            bd = CustomDimension(name='db%d' % mapper.nbuffers,
                                 symbolic_size=b.compute_size())
            dims = list(f.dimensions)
            try:
                dims[f.dimensions.index(d)] = bd
            except ValueError:
                dims.insert(0, bd)
            mapper[f].buffer = Array(name='%sb' % f.name, dimensions=dims, dtype=f.dtype)

        # Create Eqs to initialize `bf` if `f` is a read-write Function
        init = []
        for f, b in mapper.items():
            if b.is_readonly:
                continue
            for i in range(b.size()):
                indices = list(f.dimensions)
                indices[b.bdindex] = i
                init.append(DummyEq(b.buffer[indices], f[indices]))

        # Create Eqs to dump `bf` back into `f`
        dump = []
        for c, buffereds in mapper.as_lastwrite_mapper().items():
            for b in buffereds:
                writes = list(c.scope.writes[b.function])
                if len(writes) != 1:
                    raise NotImplementedError
                write = writes.pop()
                indices = list(b.function.dimensions)
                indices[b.bdindex] = indices[b.bdindex] + 
                dum
                from IPython import embed; embed()

        # Create replacements


class BufferMapper(OrderedDict):

    def __init__(self, dim):
        super().__init__()
        self.dim = dim

    def __getitem__(self, function):
        if function not in self:
            super().__setitem__(function, Buffered(function, self.dim))
        return super().__getitem__(function)

    @property
    def nbuffers(self):
        return len([b for b in self.values() if b.buffer is not None])

    def as_lastwrite_mapper(self):
        ret = DefaultOrderedDict(list)
        for b in self.values():
            ret[b.lastwrite].append(b)
        return ret


class Buffered(object):

    """
    This is a mutable data structure, which gets updated during the compilation
    pass as more information about the required buffer are determined.

    Parameters
    ----------
    function : DiscreteFunction
        The object for which a buffer is created.
    dim : Dimension
        The Dimension along which the buffer is created.
    """

    def __init__(self, function, dim):
        self.function = function
        self.dim = dim

        self.lastwrite = None
        self.readby = []
        self.buffer = None
        self.lock = None

    @property
    def is_readonly(self):
        return self.lastwrite is None

    @property
    def is_readwrite(self):
        #TODO
        pass

    @cached_property
    def bdindex(self):
        """
        The buffer Dimension index within the buffer.
        """
        assert self.buffer is not None
        for n, d in enumerate(self.buffer.dimensions):
            if d.is_Custom:
                return n
        assert False

    @cached_property
    def mds(self):
        #TODO: cannot do range(self.size) ... I must check the slots so that
        # I have -1, 0, 1 ...
        assert self.buffer is not None
        return [ModuloDimension(self.dim, i, self.size, name='d%d' % n)
                for n, i in range(self.size)]

    @cached_property
    def size(self):
        assert self.buffer is not None:
        return self.buffer.shape[self.bdindex]

    def compute_size(self):
        slots = set()
        for c in [self.lastwrite] + self.readby:
            accesses = c.scope.getreads(self.function) + c.scope.getwrites(self.function)
            slots.update([i[self.dim] for i in accesses])
        return len(slots)

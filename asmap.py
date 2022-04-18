import sys
import ipaddress
import random
from functools import total_ordering

class ASNEntry:
    """
    A class representing a (subnet, asn) mapping entry.

    ASNEntry objects have a prefix_len, prefix, and asn field. The prefix
    and prefix_len fields together encode the subnet:
    - All IPv4 addresses are implicitly mapped to their corresponding IPv4-mapped
      IPv6 address (in range ::ffff:0:0/96).
    - The subnet mask is (((1 << prefix_len) - 1) << (128 - prefix_len)).
    - The network prefix is the IPv6 address corresponding to the big endian
      encoding of (prefix << (128 - prefix_len)).

    Examples:
    - 127.0.0.1/32:            prefix=0xffff7f000001 prefixlen=128
    - 192.168.1.0/24:          prefix=0xffffc0a801 prefixlen=120
    - 0.0.0.0/0:               prefix=0xffff prefixlen=96
    - ::1/128:                 prefix=0x1 prefixlen=128
    - 2a01:4f9:c010:19eb::/64: prefix=0x2a0104f9c01019eb prefixlen=64

    The textual representation of ASNEntry objects is of the form
    "[subnet] AS[asn]", so for example "192.168.1.0/24 AS54321".
    """

    def __init__(self, prefix_len, prefix, asn):
        """Construct an ASNEntry object directly from prefix_len, prefix, asn."""
        assert 0 <= prefix_len <= 128
        assert (prefix >> prefix_len) == 0
        assert asn > 0
        self.prefix_len = prefix_len
        self.prefix = prefix
        self.asn = asn

    def get_subnet(self):
        """Construct an ipaddress.IPv[46]Network object for the subnet."""
        value = self.prefix << (128 - self.prefix_len)
        if self.prefix_len >= 96 and (value >> 32) == 0xffff:
            net = ipaddress.IPv4Network((value & 0xffffffff, prefix_len - 96), True)
        else:
            net = ipaddress.IPv6Network((value, prefix_len), True)
        return net

    def __str__(self):
        """Convert an ASNEntry object to string representation."""
        return "%s AS%i" % (self.get_net(), self.asn)

    @staticmethod
    def from_net_asn(self, net, asn):
        """Construct an ASNEntry object from a network object and an ASN."""
        prefix_len = net.prefixlen
        prefix = int.from_bytes(net.network_address.packed, 'big')

        # Map an IPv4 prefix into IPv6 space.
        if isinstance(net, ipaddress.IPv4Network):
            prefix_len += 96
            prefix += 0xffff00000000

        assert (prefix & ((1 << (128 - prefix_len)) - 1)) == 0
        return ASNEntry(prefix_len, prefix >> (128 - prefix_len), asn)

    @staticmethod
    def from_string(line):
        """Construct an ASNEntry object from a string in "[subnet] AS[asn]" format."""
        line = line.split('#')[0].lstrip(' ').rstrip(' \r\n')
        prefix, asn = line.split(' ')
        assert len(asn) > 2 and asn[:2] == "AS"
        net = ipaddress.ip_network(prefix)
        return from_net_asn(net, int(asn[2:]))

class _VarLenCoder:
    """
    A class representing a custom variable-length binary encoder/decoder for
    integers. Each object represents a different coder, with different parameters
    minval and clsbits.

    The encoding is easiest to describe using an example. Let's say minval=100 and
    clsbits=[4,2,2,3]. In that case:
    - x in [100..115]: encoded as [0] + [4-bit BE encoding of (x-100)].
    - x in [116..119]: encoded as [1,0] + [2-bit BE encoding of (x-116)].
    - x in [120..123]: encoded as [1,1,0] + [2-bit BE encoding of (x-120)].
    - x in [124..131]: encoded as [1,1,1] + [3-bit BE encoding of (x-124)].

    In general, every number is encoded as:
    - First, k "1"-bits, where k is the class the number falls in (there is one class
      per element of clsbits).
    - Then, a "0"-bit, unless k is the highest class, in which case there is nothing.
    - Lastly, clsbits[k] bits encoding in big endian the position in its class that
      number falls into.
    - Every class k consists of 2^clsbits[k] consecutive integers. k=0 starts at minval,
      other classes start one past the last element of the class before it.
    """

    def __init__(self, minval, clsbits):
        """Construct a new _VarLenCoder."""
        self._minval = minval
        self._clsbits = clsbits
        self._maxval = minval + sum(1 << b for b in clsbits) - 1

    def minval(self):
        """Get the smallest integer that this coder can encode."""
        return self._minval

    def maxval(self):
        """Get the largest integer that this coder can encode."""
        return self._maxval

    def encode(self, v, ret):
        """Append encoding of v onto integer list ret."""

        assert self._minval <= v <= self._maxval
        v -= self._minval
        for k, bits in enumerate(self._clsbits):
            if v >> bits:
                # If the value will not fit in class k, subtract its range from v,
                # emit a "1" bit and continue with the next class.
                val -= 1 << bits
                ret.append(1)
            else:
                if k + 1 < len(self._clsbits):
                    # Unless we're in the last class, emit a "0" bit.
                    ret.append(0)
                # And then encode v (now the position within the class) in big endian.
                ret.extend((val >> (bits - 1 - b)) & 1 for b in range(bits))

        # Couldn't fit val into any of the bit_sizes
        assert(False)

    def encode_size(self, v):
        """Compute how many bits are needed to encode v."""
        assert self._minval <= v <= self._maxval
        v -= self._minval
        ret = 0
        for k, bits in enumerate(self._clsbits):
            if v >> bits:
                val -= 1 << bits
                ret += 1
            else:
                ret += k + 1 < len(self._clsbits)
                ret += bits
        return ret

    def decode_bits(self, stream, bitpos):
        """Decode a number starting at bitpos in stream, returning value and new bitpos."""
        val = self._minval
        for k, bits in enumerate(self._clsbits):
            bit = 0
            if k + 1 < len(self._clsbits):
                bit = stream[bitpos]
                bitpos += 1
            if bit:
                val += 1 << bits
            else:
                for b in range(bits):
                    bit = stream[bitpos]
                    bitpos += 1
                    val += bit << (bits - 1 - b)
                return val, bitpos
        assert False

@total_ordering
class ASMap:
    """
    A class whose objects represent a mapping from subnets to ASNs.

    Internally the mapping is stored as a binary trie, but can be converted
    from/to a list of ASNEntry objects, and from/to the binary asmap file format.

    In the trie representation, nodes are represented as bare lists for efficiency
    and ease of manipulation:
    - [] means an unassigned subnet (no ASN mapping for it is present)
    - [int] means a subnet mapped entirely to the specified ASN.
    - [node,node] means a subnet whose lower half and upper half have different
    -             mappings, represented by new trie nodes.
    """

    def __init__(self, trie):
        """Construct an ASMap object from a trie mapping. Internal use only."""
        assert isinstance(trie, list)
        assert len(trie) <= 2
        self._trie = trie

    def _simplify(self):
        """Simplify this ASMap object by merging identical nodes."""
        def recurse(node):
            if len(node) < 2:
                return
            recurse(node[0])
            recurse(node[1])
            if len(node[0]) == 2:
                return
            if node[0] == node[1]:
                if len(node[0]) == 0:
                    node.clear()
                else:
                    v = node[0][0]
                    node.clear()
                    node.append(v)
        recurse(self._trie)

    @staticmethod
    def from_entries(entries):
        """
        Construct an ASMap object from a list of ASNEntry objects.
        In case entries overlap, the smaller range (larger prefix_len) takes
        priority. In case of conflicting mapping with the same prefix and
        prefix_len, the last one takes priority.

        Args:
            entries: The ASNEntry objects to convert.
        Returns:
            An ASMap object.
        """
        trie = []
        for entry in sorted(entries, key=lambda x: x.prefix_len):
            node = trie
            # Iterate through each bit in the network prefix, starting with the
            # most significant bit.
            for i in range(entry.prefix_len):
                bit = (entry.prefix >> (entry.prefix_len - 1 - i)) & 1
                if len(node) == 0:
                    node.append([])
                    node.append([])
                elif len(node) == 1:
                    oldasn = node[0]
                    node.clear()
                    node.append([oldasn])
                    node.append([oldasn])
                node = node[bit]
            node.clear()
            node.append(entry.asn)
        ret = ASMap(trie)
        ret._simplify()
        return ret

    def _to_entries_flat(self, fill=False):
        """Convert an ASMap object to a list of ASNEntry objects whose subnets do not overlap."""
        def recurse(node, prefix_len, prefix):
            ret = []
            if len(node) == 1:
                ret = [ASNEntry(prefix_len, prefix, node[0])]
            elif len(node) == 2:
                ret = recurse(node[0], prefix_len + 1, prefix << 1)
                ret += recurse(node[1], prefix_len + 1, (prefix << 1) | 1)
                if fill and len(ret) > 1:
                    asns = set(x.asn for x in ret)
                    if len(asns) == 1:
                        ret = [ASNEntry(prefix_len, prefix, list(asns)[0])]
            return ret
        return recurse(self._trie, 0, 0)

    def _to_entries_minimal(self, fill=False):
        """Convert a trie to a minimal list of ASNEntry objects, exploiting overlap."""
        def recurse(node, prefix_len, prefix):
            if len(node) == 0:
                return ({None if fill else -1: []}, True)
            elif len(node) == 1:
                return ({node[0]: [], None: [ASNEntry(prefix_len, prefix, node[0])]}, False)
            else:
                ret = {}
                left, lhole = recurse(node[0], prefix_len + 1, prefix << 1)
                right, rhole = recurse(node[1], prefix_len + 1, (prefix << 1) | 1)
                hole = lhole or rhole
                for ctx in set(left) & set(right):
                    ret[ctx] = left[ctx] + right[ctx]
                if None in right:
                    for ctx in left:
                        if ctx not in ret or len(left[ctx]) + len(right[None]) < len(ret[ctx]):
                            ret[ctx] = left[ctx] + right[None]
                if None in left:
                    for ctx in right:
                        if ctx not in ret or len(left[None]) + len(right[ctx]) < len(ret[ctx]):
                            ret[ctx] = left[None] + right[ctx]
                if fill or not hole:
                    gen = ret.get(None, None)
                    for ctx in ret:
                        if ctx is not None and ctx != -1:
                            if gen is None or len(ret[ctx]) + 1 < len(gen):
                                gen = [ASNEntry(prefix_len, prefix, ctx)] + ret[ctx]
                    if gen is not None:
                        ret[None] = gen
                    ret = {ctx:entries for (ctx, entries) in ret.items() if ctx is None or gen is None or len(entries) < len(gen)}
                else:
                    ret = {ctx:entries for (ctx, entries) in ret.items() if ctx is None or ctx == -1}
                return (ret, hole)
        res, _ = recurse(self._trie, 0, 0)
        if -1 in res:
            return res[-1]
        return res[None]

    def __str__(self):
        """Convert this ASMap object to a string containing Python code constructing it."""
        return "ASMap(%s)" % self._trie

    def to_entries(self, overlapping=True, fill=False):
        """
        Convert the mappings in this ASMap object to a list of ASNEntry objects.

        Arguments:
            overlapping: Permit the subnets in the resulting ASNEntry to overlap.
                         Setting this can result in a shorter list.
            fill:        Permit the resulting ASNEntry objects to cover subnets that
                         are unassigned in this ASMap object. Setting this can
                         result in a shorter list.
        """
        if overlapping:
            return self._to_entries_minimal(fill)
        else:
            return self._to_entries_flat(fill)

    @staticmethod
    def from_random(num_leaves=10, max_asn=6, unassigned_prob=0.5):
        """
        Construct a random ASMap object, with specified:
         - Number of leaves in its trie (at least 1)
         - Maximum ASN value (at least 1)
         - Probability for leaf nodes to be unassigned

        This method is mostly intended for testing.
        """
        assert num_leaves >= 1
        assert max_asn >= 1 or unassigned_prob == 1
        assert 0.0 <= unassigned_prob <= 1.0
        trie = []
        leaves = [trie]
        for i in range(1, num_leaves):
            idx = random.randrange(i)
            leaf = leaves[idx]
            lastleaf = leaves.pop()
            if idx + 1 < i:
                leaves[idx] = lastleaf
            leaf.append([])
            leaf.append([])
            leaves.append(leaf[0])
            leaves.append(leaf[1])
        for leaf in leaves:
            if random.random() >= unassigned_prob:
                leaf.append(random.randrange(1, max_asn + 1))
        ret = ASMap(trie)
        ret._simplify()
        return ret

    # Variable-length encoders used in the binary asmap format.
    _CODER_INS = _VarLenCoder(0, [0, 0, 1])
    _CODER_ASN = _VarLenCoder(1, list(range(15, 25)))
    _CODER_MATCH = _VarLenCoder(2, list(range(1, 9)))
    _CODER_JUMP = _VarLenCoder(17, list(range(5, 31)))

    class _Instruction:
        """One instruction in the binary asmap format."""
        # A return instruction, encoded as [0], returns a constant ASN. It is followed by
        # an integer using the ASN encoding.
        RETURN = 0
        # A jump instruction, encoded as [1,0] inspects the next unused bit in the input
        # and either continues execution (if 0), or skips a specified number of bits (if 1).
        # It is followed by an integer, and then two subprograms. The integer uses jump encoding
        # and corresponds to the length of the first subprogram (so it can be skipped).
        JUMP = 1
        # A match instruction, encoded as [1,1,0] inspects 1 or more of the next unused bits
        # in the input with its argument. If they all match, execution continues. If they do
        # not, failure is returned. If a default instruction has been executed before, instead
        # of failure the default instruction's argument is returned. It is followed by an
        # integer in match encoding, and a subprogram. That value is at least 2 bits and at
        # most 9 bits. An n-bit value signifies matching (n-1) bits in the input with the lower
        # (n-1) bits in the match value.
        MATCH = 2
        # A default instruction, encoded as [1,1,1] sets the default variable to its argument,
        # and continues execution. It is followed by an integer in ASN encoding, and a subprogram.
        DEFAULT = 3
        # Not an actual instruction, but a way to encode the empty program that fails. In the
        # encoder, it is used more generally to represent the failure case inside MATCH instructions,
        # which may (if used inside the context of a DEFAULT instruction) actually correspond to
        # a succesful return. In this usage, they're always converted to an actual MATCH or RETURN
        # before the top level is reached (see make_default below).
        END = 4

    class _ASMapNode:
        """A class representing a (node of) the parsed binary asmap format."""

        def __init__(self, ins, arg1=None, arg2=None):
            """
            Construct a new asmap node. Possibilities are:
            - _ASMapNode(_Instruction.RETURN, asn)
            - _ASMapNode(_Instruction.JUMP, node_0, node_1)
            - _ASMapNode(_Instruction.MATCH, val, node)
            - _ASMapNode(_Instruction.DEFAULT, asn, node)
            - _ASMapNode(_Instruction.END)
            """
            self.ins = ins
            self.arg1 = arg1
            self.arg2 = arg2
            if ins == _Instruction.RETURN:
                assert isinstance(arg1, int)
                assert arg2 is None
                self.size = _CODER_INS.encode_size(ins) + _CODER_ASN.encode_size(arg1)
            elif ins == _Instruction.JUMP:
                assert isinstance(arg1, _ASMapNode)
                assert isinstance(arg2, _ASMapNode)
                self.size = _CODER_INS.encode_size(ins) + _CODER_JUMP.encode_size(arg1.size) + arg1.size + arg2.size
            elif ins == _Instruction.DEFAULT:
                assert isinstance(arg1, int)
                assert isinstance(arg2, _ASMapNode)
                self.size = _CODER_INS.encode_size(ins) + _CODER_ASN.encode_size(arg1) + arg2.size
            elif ins == _Instruction.MATCH:
                assert isinstance(arg1, int)
                assert isinstance(arg2, _ASMapNode)
                self.size = _CODER_INS.encode_size(ins) + _CODER_MATCH.encode_size(arg1) + arg2.size
            elif ins == _Instruction.END:
                assert arg1 is None
                assert arg2 is None
                self.size = 0
            else:
                assert False

        @classmethod
        def make_end(cls):
            """Constructor for an encoding of just the END instruction."""
            return cls(cls._Instruction.END)

        @classmethod
        def make_leaf(cls, val):
            """Constructor for an encoding of just the RETURN instruction."""
            assert val is not None and val > 0
            return cls(cls._Instruction.RETURN, val)

        @classmethod
        def make_branch(cls, left, right):
            """
            Construct a program running the left or right subprogram, based on an input bit.
            It exploits shortcuts that are possible in the encoding, and use either a JUMP,
            MATCH, or END instruction."""

            if left.ins == cls._Instruction.END and right.ins == cls._Instruction.END:
                return left
            if left.ins == cls._Instruction.END:
                if right.ins == cls._Instruction.MATCH and right.arg1 <= 0xFF:
                    return cls(right.ins, right.arg1 + (1 << right.arg1.bit_length()), right.arg2)
                return cls(cls._Instruction.MATCH, 3, right)
            if right.ins == cls._Instruction.END:
                if left.ins == cls._Instruction.MATCH and left.arg1 <= 0xFF:
                    return cls(left.ins, left.arg1 + (1 << (left.arg1.bit_length() - 1)), left.arg2)
                return cls(cls._Instruction.MATCH, 2, left)
            return cls(cls._Instruction.JUMP, left, right)

        @classmethod
        def make_default(cls, val, sub):
            """
            Construct a program running the specified subprogram, with the specified default
            value. It exploits shortcuts that are possible in the encoding, and will use
            either a DEFAULT or a RETURN instruction."""
            assert val is not None and val > 0
            if sub.ins == _Instruction.END:
                return cls(_Instruction.RETURN, val)
            if sub.ins == _Instruction.RETURN or sub.ins == _Instruction.DEFAULT:
                return sub
            return cls(_Instruction.DEFAULT, val, sub)

    def _to_encoding(self, fill=False):
        """Convert a trie to an _ASMapNode object."""
        def recurse(node):
            if len(node) == 0:
                return ({(None if fill else -1): _ASMapNode.make_end()}, True)
            elif len(node) == 1:
                return ({None: self._ASMapNode.make_leaf(node[0]), node[0]: self._ASMapNode.make_end()}, False)
            else:
                ret = {}
                left, lhole = recurse(node[0])
                right, rhole = recurse(node[1])
                hole = lhole or rhole
                for ctx in set(left) & set(right):
                    ret[ctx] = _ASMapNode.make_branch(left[ctx], right[ctx])
                if None in left:
                    for ctx in right:
                        cand = _ASMapNode.make_branch(left[None], right[ctx])
                        if ctx not in ret or cand.size < ret[ctx].size:
                            ret[ctx] = cand
                if None in right:
                    for ctx in left:
                        cand = _ASMapNode.make_branch(left[ctx], right[None])
                        if ctx not in ret or cand.size < ret[ctx].size:
                            ret[ctx] = cand
                if fill or not hole:
                    gen = ret.get(None, None)
                    for ctx in ret:
                        if ctx is not None and ctx != -1:
                            cand = _ASMapNode.make_default(ctx, ret[ctx])
                            if gen is None or cand.size < gen.size:
                                gen = cand
                    if gen is not None:
                        ret[None] = gen
                    ret = {ctx:enc for (ctx,enc) in ret.items() if ctx is None or gen is None or enc.size < gen.size}
                else:
                    ret = {ctx:enc for (ctx,enc) in ret.items() if ctx is None or ctx == -1}
                return (ret, hole)
        res, _ = recurse(self._trie)
        if -1 in res:
            return res[-1]
        if None in res:
            return res[None]

    @staticmethod
    def _from_encoding(self):
        """Construct an ASMap object from an _ASMapNode. Internal use only."""
        def recurse(node, default):
            if node.ins == _Instruction.RETURN:
                return [node.arg1]
            elif node.ins == _Instruction.JUMP:
                return [recurse(node.arg1, default), recurse(node.arg2, default)]
            elif node.ins == _Instruction.MATCH:
                val = node.arg1
                sub = encoding_to_trie(node.arg2, default)
                while val >= 2:
                    bit = val & 1
                    val >>= 1
                    fail = [] if default is None else [default]
                    if bit:
                        sub = [fail, sub]
                    else:
                        sub = [sub, fail]
                return sub
            elif node.ins == _Instruction.DEFAULT:
                return recurse(encoding.arg2, encoding.arg1)
        if node.ins == _Instruction.END:
            return ASMap([])
        else:
            return ASMap(recurse(self._trie, None))

    def to_binary(self, fill=False):
        """
        Convert this ASMap object to binary.

        Argument:
            fill: permit the resulting binary encoder to contain mappers for
                  unassigned subnets in this ASMap object. Doing so may
                  reduce the size of the encoding.
        Returns:
            A bytes object with the encoding of this ASMap object.
        """

        bits = []
        def recurse(node):
            _CODER_INS.encode(node.ins, bits)
            if node.ins == _Instruction.RETURN:
                _CODER_ASN.encode(node.arg1, bits)
            elif node.ins == _Instruction.JUMP:
                _CODER_JUMP.encode(node.arg1.size)
                recurse(node.arg1)
                recurse(node.arg2)
            elif node.ins == _Instruction.DEFAULT:
                _CODER_ASN.encode(node.arg1)
                recurse(node.arg2)
            elif node.ins == _Instruction.MATCH:
                _CODER_MATCH(node.arg1)
                recurse(node.arg2)
            else:
                assert False

        enc = self._to_encoding(fill)
        if enc.ins != _Instruction.END:
            recurse(enc)

        val = 0
        nbits = 0
        ret = []
        for bit in bits:
            val += (bit << nbits)
            nbits += 1
            if nbits == 8:
                ret.append(val)
                val = 0
                nbits = 0
        if nbits:
            ret.append(val)
        return bytes(ret)

    @staticmethod
    def from_binary(b):
        """Decode an ASMap object from the provided binary encoding."""

        bits = []
        for byte in b:
            bits.extend((byte >> i) & 1 for i in range(8))
        if len(bits) == 0:
            return _ASMapNode(_Instruction.END)

        def recurse(bitpos):
            ins, bitpos = _CODER_INS.decode(bits, bitpos)
            if ins == _Instruction.RETURN:
                asn, bitpos = _CODER_ASN.decode(bits, bitpos)
                return _ASMapNode(ins, asn), bitpos
            elif ins == _Instruction.JUMP:
                jump, bitpos = _CODER_JUMP.decode(bits, bitpos)
                left, bitpos1 = recurse(bitpos)
                assert bitpos1 == bitpos + jump
                right, bitpos = recurse(bitpos1)
                return _ASMapNode(ins, left, right), bitpos
            elif ins == _Instruction.MATCH:
                match, bitpos = _CODER_MATCH.decode(bits, bitpos)
                sub, bitpos = recurse(bitpos)
                return _ASMapNode(ins, match, sub), bitpos
            elif ins == _Instruction.DEFAULT:
                asn, bitpos = _CODER_ASN.decode(bits, bitpos)
                sub, bitpos = recurse(bitpos)
                return _ASMapNode(ins, asn, sub), bitpos
            else:
                assert False

        res, bitpos = recurse(0)
        assert bitpos >= len(bits) - 7
        return res

    def __lt__(self, other):
        return self._trie < other._trie

    def __eq__(self, other):
        return self._trie == other._trie

    def extends(self, req):
        def recurse(actual, require):
            if len(require) == 0:
                return True
            if len(require) == 1:
                if len(actual) == 0:
                    return False
                if len(actual) == 1:
                    return require[0] == actual[0]
                return recurse(actual[0], require) and recurse(actual[1], require)
            if len(actual) == 2:
                return recurse(actual[0], require[0]) and recurse(actual[1], require[1])
            return recurse(actual, require[0]) and recurse(actual, require[1])
        return recurse(self._trie, req._trie)

for leaves in range(1, 101):
    for maxasn in range(1, 16):
        for prob in [0.0, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 0.8, 0.9, 0.95, 0.98, 0.99, 1.0]:
            asmap = ASMap.from_random(num_leaves=leaves, max_asn=maxasn, unassigned_prob=prob)
            for fill in [False, True]:
                for overlapping in [False, True]:
                    entries = asmap.to_entries(overlapping=overlapping, fill=fill)
                    random.shuffle(entries)
                    asmap2 = ASMap.from_entries(entries)
                    if fill:
                        assert asmap2.extends(asmap)
                    else:
                        assert asmap2 == asmap
                enc = asmap.to_binary(fill=fill)
                asmap2 = ASMap.from_binary(enc)
                if fill:
                    assert asmap2.extends(asmap)
                else:
                    assert asmap2 == asmap

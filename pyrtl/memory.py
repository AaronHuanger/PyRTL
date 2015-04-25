"""
Defines MemBlock, a block of memory that can be read (async) and written (sync)
"""

import collections
import core
import wire
import helperfuncs
import conditional


# ------------------------------------------------------------------------
#
#         ___        __   __          __        __   __
#   |\/| |__   |\/| /  \ |__) \ /    |__) |    /  \ /  ` |__/
#   |  | |___  |  | \__/ |  \  |     |__) |___ \__/ \__, |  \
#


# MemBlock supports any number of the following operations:
# read: d = mem[address]
# write: mem[address] = d
# write with an enable: mem[address] = MemBlock.EnabledWrite(d,enable=we)
# Based on the number of reads and writes a memory will be inferred
# with the correct number of ports to support that

# _MemAssignment is the type returned from assignment by |= or <<=
_MemAssignment = collections.namedtuple('_MemAssignment', 'rhs, is_conditional')


class _MemIndexed(wire.WireVector):
    """ Object used internally to route memory assigns correctly.

    The normal PyRTL user should never need to be aware that this class exists,
    hence the underscore in the name.  It presents a very similar interface to
    wiresVectors (all of the normal wirevector operations should still work),
    but if you try to *set* the value with <<= or |= then it will generate a
    _MemAssignment object rather than the normal wire assignment. """

    def __init__(self, mem, index):
        self.mem = mem
        self.index = index

    def __ilshift__(self, other):
        return _MemAssignment(rhs=other, is_conditional=False)

    def __ior__(self, other):
        return _MemAssignment(rhs=other, is_conditional=True)

    def logicop(self, other, op):
        return helperfuncs.as_wires(self).logicop(other, op)

    def __invert__(self):
        return helperfuncs.as_wires(self).__invert__()

    def __getitem__(self, item):
        return helperfuncs.as_wires(self).__getitem__(item)

    def __len__(self):
        return self.mem.bitwidth

    def sign_extended(self, bitwidth):
        return helperfuncs.as_wires(self).sign_extended(bitwidth)

    def zero_extended(self, bitwidth):
        return helperfuncs.as_wires(self).zero_extended(bitwidth)


class _MemReadBase(object):
    """This is the base class for the memories and ROM blocks and
    it implements the read and initialization operations needed for
    both of them"""

    # FIXME: right now read port as build unconditionally

    def __init__(self,  bitwidth, addrwidth, name=None, block=None):

        self.block = core.working_block(block)
        name = core.next_tempvar_name(name)

        if bitwidth <= 0:
            raise core.PyrtlError('error, bitwidth must be >= 1')
        if addrwidth <= 0:
            raise core.PyrtlError('error, addrwidth must be >= 1')

        self.bitwidth = bitwidth
        self.name = name
        self.addrwidth = addrwidth
        self.readport_nets = []
        self.id = core.next_memid()

    def __getitem__(self, item):
        from helperfuncs import as_wires
        item = as_wires(item, bitwidth=self.addrwidth, truncating=False, block=self.block)
        if len(item) > self.addrwidth:
            raise core.PyrtlError('error, memory index bitwidth > addrwidth')
        return _MemIndexed(mem=self, index=item)

    def _readaccess(self, addr):
        # FIXME: add conditional read ports
        return self._build_read_port(addr)

    def _build_read_port(self, addr):
        data = wire.WireVector(bitwidth=self.bitwidth, block=self.block)
        readport_net = core.LogicNet(
            op='m',
            op_param=(self.id, self),
            args=(addr,),
            dests=(data,))
        self.block.add_net(readport_net)
        self.readport_nets.append(readport_net)
        return data

    def __setitem__(self, key, value):
        raise core.PyrtlInternalError("error, invalid call __setitem__ made on _MemReadBase")

    def _make_copy(self, block):
        pass


class MemBlock(_MemReadBase):
    """ An object for specifying read and write enabled block memories """
    # FIXME: write ports assume that only one port is under control of the conditional

    EnabledWrite = collections.namedtuple('EnabledWrite', 'data, enable')

    # data <<= memory[addr]  (infer read port)
    # memory[addr] <<= data  (infer write port)
    def __init__(self, bitwidth, addrwidth, name=None, block=None):
        super(MemBlock, self).__init__(bitwidth, addrwidth, name, block)
        self.writeport_nets = []

    def __setitem__(self, item, assignment):
        if isinstance(assignment, _MemAssignment):
            self._assignment(item, assignment.rhs, is_conditional=assignment.is_conditional)
        else:
            raise core.PyrtlError('error, assigment to memories should use "<<=" not "=" operator')

    def _assignment(self, item, val, is_conditional):
        from helperfuncs import as_wires
        item = as_wires(item, bitwidth=self.addrwidth, truncating=False, block=self.block)
        if len(item) > self.addrwidth:
            raise core.PyrtlError('error, memory index bitwidth > addrwidth')
        addr = item

        if isinstance(val, MemBlock.EnabledWrite):
            data, enable = val.data, val.enable
        else:
            data, enable = val, wire.Const(1, bitwidth=1, block=self.block)
        data = as_wires(data, bitwidth=self.bitwidth, truncating=False, block=self.block)
        enable = as_wires(enable, bitwidth=1, truncating=False, block=self.block)

        if len(data) != self.bitwidth:
            raise core.PyrtlError('error, write data larger than memory  bitwidth')
        if len(enable) != 1:
            raise core.PyrtlError('error, enable signal not exactly 1 bit')

        if is_conditional:
            conditional.ConditionalUpdate._build_write_port(self, addr, data, enable)
        else:
            self._build_write_port(addr, data, enable)

    def _build_write_port(self, addr, data, enable):
        writeport_net = core.LogicNet(
            op='@',
            op_param=(self.id, self),
            args=(addr, data, enable),
            dests=tuple())
        self.block.add_net(writeport_net)
        self.writeport_nets.append(writeport_net)

    def _make_copy(self, block=None):
        if block is None:
            block = self.block
        return MemBlock(self.bitwidth, self.addrwidth, self.name, block)


class RomBlock(_MemReadBase):
    """ RomBlocks are the read only memory format in PYRTL
        By default, they synthesize down to transistor-based
        logic during synthesis


    """
    def __init__(self, bitwidth, addrwidth, data, name=None, block=None):
        """
        :param bitwidth: The bitwidth of the parameters
        :param addrwidth: The bitwidth of the address bus
        :param data: This can either be a function or an array that maps
        an address as an input to a result as an output
        """
        super(RomBlock, self).__init__(bitwidth, addrwidth, name, block)
        self.data = data

    def _get_read_data(self, address):
        import types
        if address < 0 or address > 2**self.addrwidth - 1:
            raise core.PyrtlError("Error: Invalid address, " + str(address) + " specified")
        if isinstance(self.data, types.FunctionType):
            try:
                value = self.data(address)
            except Exception:
                raise core.PyrtlError("Invalid data function for RomBlock")
        else:
            try:
                value = self.data[address]
            except TypeError:
                raise core.PyrtlError("invalid type for RomBlock data object")
            except IndexError:
                raise core.PyrtlError("An access to rom " + self.name +
                                      " index " + str(address) + " is out of range")

        if value < 0 or value >= 2**self.bitwidth:
            raise core.PyrtlError("invalid value for RomBlock data")

        return value

    def _make_copy(self, block=None):
        if block is None:
            block = self.block
        return RomBlock(self.bitwidth, self.addrwidth, self.data, self.name, block,)

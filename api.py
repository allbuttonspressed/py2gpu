import ast
from functools import wraps
import inspect
import numpy
import pycuda.autoinit
from pycuda import driver
from pycuda.compiler import SourceModule
from pycuda.gpuarray import splay
from py2gpu.grammar import _gpu_funcs, convert, make_prototype

def _simplify(mapping):
    simple = {}
    for names, value in mapping.items():
        if not isinstance(names, (list, tuple, set)):
            names = (names,)
        for name in names:
            assert name not in simple, "Variable %s specified multiple times" % name
            simple[name] = value
    return simple

def _rename_func(tree, name):
    tree.body[0].name = name
    return tree

def blockwise(blockshapes, types, overlapping=True, center_as_origin=False, name=None):
    blockshapes = _simplify(blockshapes)
    types = _simplify(types)
    assert overlapping or not center_as_origin, \
        "You can't have overlapping=False and center_as_origin=True"
    for varname, vartype in types.items():
        types[varname] = get_arg_type(vartype)
    def _blockwise(func):
        func = getattr(func, '_py2gpu_original', func)
        fname = name
        if not fname:
            fname = func.__name__
        assert fname not in _gpu_funcs, 'The function "%s" has already been registered!' % fname
        info = _gpu_funcs.setdefault(fname, {
            'func': func,
            'functype': 'blockwise',
            'blockshapes': blockshapes,
            'overlapping': overlapping,
            'center_as_origin': center_as_origin,
            'types': types,
            'tree': _rename_func(ast.parse(inspect.getsource(func)), fname),
            'prototypes': {},
        })
        def _call_blockwise(*args):
            assert 'gpufunc' in info, \
                'You have to call compile_gpu_code() before executing a GPU function.'
            gpufunc = info['gpufunc']
            return gpufunc(*args)
        _call_blockwise._py2gpu_original = func
        return wraps(func)(_call_blockwise)
    return _blockwise

_typedef_base = r'''
typedef struct __align__(64) {
    %(type)s *data;
    int dim[4];
    int offset[4];
    int ndim;
    int size;
} %(Type)sArrayStruct;
typedef %(Type)sArrayStruct* %(Type)sArray;
'''.lstrip()

_typedefs = r'''
#define sync __synchronize

'''.lstrip() + '\n'.join(_typedef_base % {'type': name, 'Type': name.capitalize()}
                for name in ['int', 'float']) + '\n\n'

intpsize = numpy.intp(0).nbytes
int32size = numpy.int32(0).nbytes

class GPUArray(object):
    size = intpsize + (4 + 4 + 1 + 1) * int32size
    def __init__(self, data):
        self.data = data

        # Copy array to device
        self.pointer = driver.mem_alloc(self.size)

        # data
        self.device_data = driver.to_device(data)
        driver.memcpy_htod(int(self.pointer), numpy.intp(int(self.device_data)))

        # dim
        struct = data.shape
        struct += (4 - data.ndim) * (0,)
        # offset, ndim, size
        struct += 4 * (0,) + (data.ndim, data.size)
        struct = numpy.array(struct, dtype=numpy.int32)
        driver.memcpy_htod(int(self.pointer) + intpsize, buffer(struct))

    def copy_to_host(self):
        self.data[...] = driver.from_device_like(self.device_data, self.data)

class ArrayType(object):
    pass

class IntArray(ArrayType):
    pass

class FloatArray(ArrayType):
    pass

def get_arg_type(arg):
    if issubclass(arg, ArrayType):
        return "P", arg.__name__
    if issubclass(arg, numpy.int8):
        return "b", 'char'
    if issubclass(arg, numpy.uint8):
        return "B", 'unsigned char'
    if issubclass(arg, numpy.int16):
        return "h", 'short'
    if issubclass(arg, numpy.uint16):
        return "H", 'unsigned short'
    if issubclass(arg, (int, numpy.int32)):
        return "i", 'int'
    if issubclass(arg, numpy.uint32):
        return "I", 'unsigned int'
    if issubclass(arg, (long, numpy.int64)):
        return "l", 'long'
    if issubclass(arg, numpy.uint64):
        return "L", 'unsigned long'
    if issubclass(arg, (float, numpy.float32)):
        return "f", 'float'
    if issubclass(arg, numpy.float64):
        return "d", 'double'
    raise ValueError("Unknown type '%r'" % tp)

def make_gpu_func(mod, name, info):
    func = mod.get_function('_kernel_' + name)
    blockshapes = info['blockshapes']
    overlapping = info['overlapping']
    center_as_origin = info['center_as_origin']
    types = info['types']
    argnames = inspect.getargspec(info['func']).args
    argtypes = ''.join(types[arg][0] for arg in argnames) + 'i'
    func.prepare(argtypes, (1, 1, 1))
    def _gpu_func(*args):
        kernel_args = []
        arrays = []
        grid = (1, 1, 1)
        count, block = 0, 0
        for argname, arg in zip(argnames, args):
            if isinstance(arg, numpy.ndarray):
                arg = GPUArray(arg)
                arrays.append(arg)
            if isinstance(arg, GPUArray):
                shape = blockshapes.get(argname)
                if shape:
                    if overlapping and shape == (1,) * len(shape):
                        # TODO: read pixels into shared memory by running
                        # shape[1:].prod() threads that read contiguous lines
                        # of memory, sync(), and then let only the first
                        # thread in the block do the real calculation
                        # TODO: maybe we can reorder data if it's read-only?
                        if center_as_origin:
                            assert all(dim % 2 for dim in shape), \
                                'Block dimensions must be uneven when using ' \
                                'center_as_origin=True. Please check %s' % argname
                            blockcount = arg.data.size
                        else:
                            blockcount = (numpy.array(arg.data.shape) -
                                          (numpy.array(shape) - 1)).prod()
                    else:
                        assert not any(dim1 % dim2 for dim1, dim2
                                       in zip(arg.data.shape, shape)), \
                            'Size of argument "%s" must be an integer ' \
                            'multiple of its block size when using ' \
                            'non-overlapping blocks.' % argname
                        # TODO: reorder pixels for locality
                        blockcount = arg.data.size / numpy.array(shape).prod()
                    if count:
                        assert count == blockcount, \
                            'Number of blocks of argument "%s" ' \
                            "doesn't match the preceding blockwise " \
                            'arguments.' % argname
                    count = blockcount
                arg = arg.pointer
            kernel_args.append(arg)
        # Determine number of blocks
        kernel_args.append(count)
        grid, block = splay(count)
        func.set_block_shape(*block)
        func.prepared_call(grid, *kernel_args)
        # Now copy temporary arrays back
        for gpuarray in arrays:
            # TODO: reverse reordering if needed
            gpuarray.copy_to_host()
    return _gpu_func

def make_emulator_func(func):
    def emulator(*args):
        assert len(kwargs.keys()) == 1, 'Only "block" keyword argument is supported!'
        # TODO:
        raise NotImplementedError('Function emulation is not supported, yet')
    return emulator

def compile_gpu_code():
    source = ['\n\n']
    for name, info in _gpu_funcs.items():
        source.append(convert(info['tree']))
        source.insert(0, ';\n'.join(info['prototypes'].values()) + ';\n')
    source.insert(0, _typedefs)
    print ''.join(source)
    mod = SourceModule('\n'.join(source))
    for name, info in _gpu_funcs.items():
        info['gpufunc'] = make_gpu_func(mod, name, info)

def emulate_gpu_code():
    for info in _gpu_funcs.values():
        info['gpufunc'] = make_emulator_func(info['func'])

/* CPython block parser. Drive I/O, reconciliation, UI and AI stay in Python. */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdint.h>
#include <string.h>

static uint16_t r16(const unsigned char *p) { uint16_t n; memcpy(&n,p,2); return n; }
static uint32_t r32(const unsigned char *p) { uint32_t n; memcpy(&n,p,4); return n; }
static uint64_t r64(const unsigned char *p) { uint64_t n; memcpy(&n,p,8); return n; }
static uint64_t positive(const unsigned char *p) { int64_t n; memcpy(&n,p,8); return n<0 ? 0 : (uint64_t)n; }
static int within(size_t n,size_t offset,size_t length) { return offset<=n && length<=n-offset; }

/* Bounded per-block sharing: immutable metadata integers often repeat in
   neighbouring records. No global cache or retained reference to a prior scan. */
typedef struct { uint64_t value; PyObject *number; } NumberCache;
typedef struct { NumberCache parent,modified,allocated; } BlockCache;

static PyObject *cached_number(NumberCache *cache,uint64_t value) {
    if(value<=256) return PyLong_FromUnsignedLongLong(value);
    if(cache->number && cache->value==value) return Py_NewRef(cache->number);
    PyObject *number=PyLong_FromUnsignedLongLong(value);
    if(!number) return NULL;
    cache->value=value;
    Py_XSETREF(cache->number,Py_NewRef(number));
    return number;
}

static int fixup(unsigned char *d,size_t n) {
    if(n<8) return 0;
    size_t offset=r16(d+4),count=r16(d+6);
    if(count<2 || !within(n,offset,count*2)) return 0;
    uint16_t signature=r16(d+offset);
    for(size_t i=1;i<count;i++) {
        size_t end=i*512;
        if(end>n || r16(d+end-2)!=signature) return 0;
        uint16_t replacement=r16(d+offset+i*2);
        memcpy(d+end-2,&replacement,2);
    }
    return 1;
}

typedef struct {
    uint64_t base,parent,modified;
    uint32_t attributes;
    uint16_t sequence,links;
    int directory,attribute_list;
    PyObject *name,*names,*streams,*logical,*allocated;
    PyObject *first_name;
    uint64_t first_parent,stream_logical,stream_allocated;
    uint32_t stream_kind;
    const unsigned char *stream_name;
    size_t stream_name_length;
    int has_stream;
} Parsed;

static void clear_parsed(Parsed *p) {
    Py_XDECREF(p->name); Py_XDECREF(p->names); Py_XDECREF(p->streams);
    Py_XDECREF(p->logical); Py_XDECREF(p->allocated);
    Py_XDECREF(p->first_name);
}

/* Returns 1 for sizes, 0 for a continuation/short attribute. */
static int sizes(const unsigned char *a,size_t n,uint64_t *logical,uint64_t *allocated) {
    if(a[8]) {
        if(n<0x40 || r64(a+0x10)!=0) return 0;
        size_t at=(r16(a+0x0c)&0x8001) ? 0x40 : 0x28;
        if(!within(n,at,8)) return 0;
        *logical=positive(a+0x30); *allocated=positive(a+at);
    } else {
        if(n<0x18) return 0;
        *logical=r32(a+0x10); *allocated=0;
    }
    return 1;
}

static int set_stream(Parsed *p,uint32_t kind,const unsigned char *name,size_t length,
                      uint64_t logical,uint64_t allocated) {
    PyObject *key=Py_BuildValue("(Iy#)",kind,(const char *)name,(Py_ssize_t)length);
    PyObject *value=Py_BuildValue("(KK)",(unsigned long long)logical,(unsigned long long)allocated);
    if(!key || !value) { Py_XDECREF(key); Py_XDECREF(value); return -1; }
    int result=PyDict_SetItem(p->streams,key,value);
    Py_DECREF(key); Py_DECREF(value); return result;
}

static int materialize_streams(Parsed *p) {
    if(p->streams) return 0;
    p->streams=PyDict_New();
    if(!p->streams) return -1;
    return p->has_stream ? set_stream(p,p->stream_kind,p->stream_name,p->stream_name_length,
                                      p->stream_logical,p->stream_allocated) : 0;
}

static int add_stream(Parsed *p,uint32_t kind,const unsigned char *name,size_t length,
                      uint64_t logical,uint64_t allocated) {
    /* Keep a single stream in C fields; materialize only for multiple streams
       or retained extension/link details. The input block stays pinned. */
    if(!p->streams && (!p->has_stream || (kind==p->stream_kind && length==p->stream_name_length &&
            (!length || memcmp(name,p->stream_name,length)==0)))) {
        p->has_stream=1; p->stream_kind=kind; p->stream_name=name; p->stream_name_length=length;
        p->stream_logical=logical; p->stream_allocated=allocated;
        return 0;
    }
    if(materialize_streams(p)<0) return -1;
    return set_stream(p,kind,name,length,logical,allocated);
}

static int materialize_names(Parsed *p) {
    if(p->names) return 0;
    p->names=PyList_New(0);
    if(!p->names) return -1;
    if(p->first_name) {
        PyObject *pair=Py_BuildValue("(KO)",(unsigned long long)p->first_parent,p->first_name);
        if(!pair) return -1;
        int status=PyList_Append(p->names,pair); Py_DECREF(pair); return status;
    }
    return 0;
}

static int add_name(Parsed *p,uint64_t parent,PyObject *name) {
    if(!p->first_name && !p->names) {
        p->first_parent=parent; p->first_name=Py_NewRef(name); return 0;
    }
    if(!p->names && parent==p->first_parent) {
        int equal=PyObject_RichCompareBool(name,p->first_name,Py_EQ);
        if(equal<0) return -1;
        if(equal) return 0;
    }
    if(materialize_names(p)<0) return -1;
    PyObject *pair=Py_BuildValue("(KO)",(unsigned long long)parent,name);
    if(!pair) return -1;
    int present=PySequence_Contains(p->names,pair);
    int status=present<0 || (!present && PyList_Append(p->names,pair)<0) ? -1 : 0;
    Py_DECREF(pair); return status;
}

/* 1=parsed, 0=invalid record, -1=Python exception. All offsets are checked. */
static int parse(const unsigned char *d,size_t n,Parsed *p,BlockCache *cache) {
    if(n<0x30 || memcmp(d,"FILE",4)!=0 || !(r16(d+0x16)&1)) return 0;
    p->sequence=r16(d+0x10); p->base=r64(d+0x20);
    p->directory=!!(r16(d+0x16)&2); p->links=r16(d+0x12);
    if(!p->links) p->links=1;
    p->name=PyUnicode_New(0,0);
    if(!p->name) return -1;
    size_t limit=r32(d+0x18); if(limit>n) limit=n;
    size_t offset=r16(d+0x14);
    int rank=-1,has_reparse=0;
    uint32_t reparse=0;
    while(within(limit,offset,16)) {
        uint32_t kind=r32(d+offset);
        if(kind==UINT32_MAX) break;
        size_t length=r32(d+offset+4);
        if(length<16 || !within(limit,offset,length)) return 0;
        const unsigned char *a=d+offset;
        int nonresident=!!a[8]; size_t name_chars=a[9];
        if((kind==0x10 || kind==0xc0) && !nonresident) {
            if(length<0x16) return 0;
            size_t start=offset+r16(a+0x14),value_length=r32(a+0x10);
            if(kind==0x10 && value_length>=0x24 && within(limit,start,0x24)) {
                uint64_t timestamp=r64(d+start+8),epoch=UINT64_C(116444736000000000);
                p->modified=timestamp<epoch ? 0 : (timestamp-epoch)/10000000;
                p->attributes=r32(d+start+0x20);
            } else if(kind==0xc0 && value_length>=4 && within(limit,start,4)) {
                reparse=r32(d+start); has_reparse=1;
            }
        } else if(kind==0x20) {
            p->attribute_list=1;
            uint64_t logical,allocated;
            if(nonresident && sizes(a,length,&logical,&allocated) &&
               add_stream(p,kind,(const unsigned char *)"",0,logical,allocated)<0) return -1;
        } else if(kind==0x30 && !nonresident) {
            if(length<0x16) return 0;
            size_t start_relative=r16(a+0x14),start=offset+start_relative,value_length=r32(a+0x10);
            if(start_relative>=0x18 && value_length>=0x42 && within(n,start,value_length)) {
                uint64_t parent=r64(d+start);
                size_t chars=d[start+0x40],name_length=chars*2;
                int ns=d[start+0x41];
                if(0x42+name_length<=value_length) {
                    int candidate_rank=ns==3 ? 3 : ns==1 ? 2 : ns==0 ? 1 : 0;
                    /* A DOS-only spelling cannot replace an already retained
                       higher-ranked name and is never a hard-link alias. */
                    if(candidate_rank==0 && rank>=0) {
                        offset+=length;
                        continue;
                    }
                    int byteorder=-1;
                    PyObject *name=PyUnicode_DecodeUTF16((const char *)d+start+0x42,
                        (Py_ssize_t)name_length,"replace",&byteorder);
                    if(!name) return -1;
                    if(PyUnicode_GET_LENGTH(name)) {
                        if(candidate_rank>0) {
                            if(add_name(p,parent,name)<0) { Py_DECREF(name); return -1; }
                        }
                        if(candidate_rank>rank) {
                            rank=candidate_rank; p->parent=parent;
                            Py_SETREF(p->name,Py_NewRef(name));
                        }
                    }
                    Py_DECREF(name);
                }
            }
        } else if(kind==0x80 || nonresident) {
            size_t name_offset=r16(a+0x0a),name_length=name_chars*2;
            if(name_length && (name_offset<16 || !within(length,name_offset,name_length))) return 0;
            uint64_t logical,allocated;
            if(sizes(a,length,&logical,&allocated) && add_stream(p,kind,
                    name_length ? a+name_offset : (const unsigned char *)"",name_length,logical,allocated)<0) return -1;
        }
        offset+=length;
    }
    if(has_reparse && !(reparse&0x20000000)) p->attributes&=~0x400u;
    if(p->directory) p->attributes|=0x10;
    if(!p->streams) {
        uint64_t logical=p->has_stream && p->stream_kind==0x80 && p->stream_name_length==0 ? p->stream_logical : 0;
        p->logical=PyLong_FromUnsignedLongLong(logical);
        p->allocated=cached_number(&cache->allocated,p->has_stream ? p->stream_allocated : 0);
        if(!p->logical || !p->allocated) return -1;
        if(p->base || p->attribute_list || p->names) {
            if(materialize_names(p)<0 || materialize_streams(p)<0) return -1;
        }
        return 1;
    }
    p->logical=PyLong_FromLong(0); p->allocated=PyLong_FromLong(0);
    if(!p->logical || !p->allocated) return -1;
    Py_ssize_t pos=0; PyObject *key,*value;
    while(PyDict_Next(p->streams,&pos,&key,&value)) {
        PyObject *total=PyNumber_Add(p->allocated,PyTuple_GET_ITEM(value,1));
        if(!total) return -1;
        Py_SETREF(p->allocated,total);
        if(PyLong_AsUnsignedLong(PyTuple_GET_ITEM(key,0))==0x80 &&
                PyBytes_GET_SIZE(PyTuple_GET_ITEM(key,1))==0) {
            Py_SETREF(p->logical,Py_NewRef(PyTuple_GET_ITEM(value,0)));
        }
        if(PyErr_Occurred()) return -1;
    }
    if((p->base || p->attribute_list || p->names) && materialize_names(p)<0) return -1;
    return 1;
}

static int extend_extension(PyObject *extensions,Parsed *p) {
    PyObject *base=PyLong_FromUnsignedLongLong(p->base);
    if(!base) return -1;
    PyObject *extra=PyDict_GetItemWithError(extensions,base);
    if(!extra && !PyErr_Occurred()) {
        extra=Py_BuildValue("{s:[],s:{}}","names","streams");
        if(!extra || PyDict_SetItem(extensions,base,extra)<0) {
            Py_XDECREF(extra); Py_DECREF(base); return -1;
        }
        Py_DECREF(extra); extra=PyDict_GetItemWithError(extensions,base);
    }
    Py_DECREF(base);
    if(!extra) return -1;
    PyObject *names=PyDict_GetItemString(extra,"names"),*streams=PyDict_GetItemString(extra,"streams");
    if(!names || !streams || !PyList_Check(names) || !PyDict_Check(streams)) {
        PyErr_SetString(PyExc_TypeError,"Invalid extension state"); return -1;
    }
    return PyList_SetSlice(names,PyList_GET_SIZE(names),PyList_GET_SIZE(names),p->names)<0 ||
           PyDict_Update(streams,p->streams)<0 ? -1 : 0;
}

static int store_record(PyObject *records,PyObject *details,PyObject *record_type,
                        PyObject *attribute_values,Parsed *p,uint64_t frn,BlockCache *cache) {
    PyObject *id=PyLong_FromUnsignedLongLong(frn),*parent=cached_number(&cache->parent,p->parent),
        *attrs=PyLong_FromUnsignedLong(p->attributes),*modified=cached_number(&cache->modified,p->modified),
        *links=PyLong_FromUnsignedLong(p->links),*row=NULL,*detail=NULL;
    int result=-1;
    if(!id || !parent || !attrs || !modified || !links) goto done;
    PyObject *shared=PyDict_GetItemWithError(attribute_values,attrs);
    if(!shared) {
        if(PyErr_Occurred() || PyDict_SetItem(attribute_values,attrs,attrs)<0) goto done;
        shared=attrs;
    }
    PyObject *args[]={id,parent,p->name,shared,p->logical,p->allocated,Py_None,Py_True,modified,Py_None,links};
    row=PyObject_Vectorcall(record_type,args,11,NULL);
    if(!row || PyDict_SetItem(records,id,row)<0) goto done;
    if((p->names && PyList_GET_SIZE(p->names)>1) || p->attribute_list) {
        detail=Py_BuildValue("{s:O,s:O,s:O}","names",p->names,"streams",p->streams,
                             "attribute_list",p->attribute_list ? Py_True : Py_False);
        if(!detail || PyDict_SetItem(details,id,detail)<0) goto done;
    }
    result=0;
done:
    Py_XDECREF(id); Py_XDECREF(parent); Py_XDECREF(attrs); Py_XDECREF(modified);
    Py_XDECREF(links); Py_XDECREF(row); Py_XDECREF(detail); return result;
}

static PyObject *parse_batch(PyObject *self,PyObject *args) {
    (void)self;
    PyObject *buffer,*records,*details,*extensions,*record_type,*attributes;
    Py_ssize_t record_size; unsigned long long first;
    if(!PyArg_ParseTuple(args,"OnKOOOOO",&buffer,&record_size,&first,&records,&details,
                        &extensions,&record_type,&attributes)) return NULL;
    if(record_size<64 || record_size>65536 || record_size%512 ||
       !PyDict_Check(records) || !PyDict_Check(details) || !PyDict_Check(extensions) ||
       !PyDict_Check(attributes) || !PyCallable_Check(record_type)) {
        PyErr_SetString(PyExc_ValueError,"Invalid MFT batch arguments"); return NULL;
    }
    Py_buffer view;
    if(PyObject_GetBuffer(buffer,&view,PyBUF_WRITABLE|PyBUF_C_CONTIGUOUS)<0) return NULL;
    size_t usable=(size_t)view.len/(size_t)record_size*(size_t)record_size;
    BlockCache cache={0};
    PyObject *result=NULL;
    uint64_t skipped=0,index=first;
    for(size_t at=0;at<usable;at+=(size_t)record_size,index++) {
        unsigned char *d=(unsigned char *)view.buf+at;
        if(memcmp(d,"FILE",4)!=0 || !(r16(d+0x16)&1)) continue;
        if(!fixup(d,(size_t)record_size)) { skipped++; continue; }
        Parsed p={0};
        int status=parse(d,(size_t)record_size,&p,&cache);
        if(status<0) { clear_parsed(&p); goto done; }
        if(!status) { skipped++; clear_parsed(&p); continue; }
        int failed=0;
        if(p.base) failed=extend_extension(extensions,&p)<0;
        else if(PyUnicode_GET_LENGTH(p.name)) {
            uint64_t frn=(index&UINT64_C(0xffffffffffff))|((uint64_t)p.sequence<<48);
            failed=store_record(records,details,record_type,attributes,&p,frn,&cache)<0;
        } else skipped++;
        clear_parsed(&p);
        if(failed) goto done;
    }
    result=PyLong_FromUnsignedLongLong(skipped);
done:
    Py_XDECREF(cache.parent.number); Py_XDECREF(cache.modified.number);
    Py_XDECREF(cache.allocated.number);
    PyBuffer_Release(&view);
    return result;
}

static PyMethodDef methods[]={
    {"parse_batch",parse_batch,METH_VARARGS,"Parse complete protected MFT records into existing scan dictionaries."},
    {NULL,NULL,0,NULL}
};
static struct PyModuleDef module={PyModuleDef_HEAD_INIT,"_mft_fast",NULL,-1,methods,NULL,NULL,NULL,NULL};
PyMODINIT_FUNC PyInit__mft_fast(void) {
    PyObject *m=PyModule_Create(&module);
    if(m && PyModule_AddIntConstant(m,"PARSER_VERSION",1)<0) { Py_DECREF(m); return NULL; }
    if(m && PyModule_AddIntConstant(m,"OPTIMIZATION_VERSION",3)<0) { Py_DECREF(m); return NULL; }
    return m;
}

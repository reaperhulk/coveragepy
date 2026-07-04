/* Licensed under the Apache License: http://www.apache.org/licenses/LICENSE-2.0 */
/* For details: https://github.com/coveragepy/coveragepy/blob/main/NOTICE.txt */

#ifndef _COVERAGE_TRACER_H
#define _COVERAGE_TRACER_H

#include "util.h"
#include "structmember.h"
#include "frameobject.h"
#include "opcode.h"

#include "buffer.h"
#include "datastack.h"

/* On free-threaded builds there is no GIL to make the pure-C trace-buffer
 * operations atomic, so recording (in the traced thread) and draining (any
 * thread calling flush_data) are serialized with a per-tracer mutex.  It is
 * held only across pure-C buffer operations, never across anything that can
 * run Python code, so it cannot deadlock.  With a GIL these are no-ops.
 */
#ifdef Py_GIL_DISABLED
#define BUFFER_LOCK(self)       PyMutex_Lock(&(self)->buffer_mutex)
#define BUFFER_UNLOCK(self)     PyMutex_Unlock(&(self)->buffer_mutex)
#else
#define BUFFER_LOCK(self)
#define BUFFER_UNLOCK(self)
#endif

/* The CTracer type. */

typedef struct CTracer {
    PyObject_HEAD

    /* Python objects manipulated directly by the Collector class. */
    PyObject * should_trace;
    PyObject * check_include;
    PyObject * warn;
    PyObject * concur_id_func;
    PyObject * data;
    PyObject * file_tracers;
    PyObject * should_trace_cache;
    PyObject * trace_arcs;
    PyObject * should_start_context;
    PyObject * switch_context;
    PyObject * lock_data;
    PyObject * unlock_data;
    PyObject * disable_plugin;

    /* Has the tracer been started? */
    _Atomic BOOL started;
    /* Are we tracing arcs, or just lines? */
    BOOL tracing_arcs;
    /* Have we had any activity? */
    _Atomic BOOL activity;
    /* The current dynamic context. */
    PyObject * context;

    /* Where trace data is recorded: per-file hash sets of uint64 values,
        owned by this tracer alone.  If tracing arcs, the values are packed
        line-number pairs; if not, they are line numbers.  The buffer is
        drained into the shared Python `data` dict by flush_data().
    */
    TraceBuffer buffer;
#ifdef Py_GIL_DISABLED
    PyMutex buffer_mutex;           /* Serializes buffer access; see above. */
#endif

    /*
        The data stack parallels the call stack: each call pushes the new
        frame's buffer table onto the data stack, and each return pops it off.
    */

    DataStack data_stack;           /* Used if we aren't doing concurrency. */

    PyObject * data_stack_index;    /* Used if we are doing concurrency. */
    DataStack * data_stacks;
    int data_stacks_alloc;
    int data_stacks_used;
    DataStack * pdata_stack;

    /* The current file's data stack entry. */
    DataStackEntry * pcur_entry;

    Stats stats;
} CTracer;

int CTracer_intern_strings(void);

extern PyTypeObject CTracerType;

#endif /* _COVERAGE_TRACER_H */

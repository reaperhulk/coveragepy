/* Licensed under the Apache License: http://www.apache.org/licenses/LICENSE-2.0 */
/* For details: https://github.com/coveragepy/coveragepy/blob/main/NOTICE.txt */

#ifndef _COVERAGE_BUFFER_H
#define _COVERAGE_BUFFER_H

#include "util.h"

/* Per-tracer trace-data buffers.
 *
 * Each CTracer records line numbers (or packed arcs) into plain-C hash sets
 * of uint64 values, one per source file, owned by that tracer alone.  The
 * hot path never touches Python objects, so it needs no data lock: with the
 * GIL, every mutation here is one uninterruptible C sequence (only PyMem
 * allocation, which cannot trigger GC or release the GIL); on free-threaded
 * builds the tracer wraps these calls in its buffer mutex.
 *
 * The buffered values become Python ints in Python sets only when the
 * tracer's flush_data() drains the buffer: it "steals" the filled tables
 * (a pure-C snapshot-and-reset, see TraceBuffer_steal) and then converts
 * the stolen values at leisure, safe from concurrent recording.
 */

/* A hash set of uint64 values for one source file. */
typedef struct FileTable {
    PyObject * filename;        /* Owned.  The source filename to credit. */
    PyObject * plugin_name;     /* Owned, or NULL.  File tracer plugin name. */
    uint64 * slots;             /* Open-addressed table; 0 means empty slot. */
    size_t mask;                /* Capacity - 1.  Capacity is a power of two. */
    size_t used;                /* Number of occupied slots. */
    BOOL has_zero;              /* The value 0 can't live in a slot; flag it. */
} FileTable;

/* Maps a filename to its FileTable, keyed by pointer identity (the same
 * filename string object arrives on every call event for a file, because it
 * comes from the cached disposition).  Distinct-but-equal string objects
 * would just make two tables, merged by string equality at drain time.
 */
typedef struct FileTableIndexEntry {
    PyObject * key;             /* Borrowed: files[idx]->filename. */
    size_t idx;
} FileTableIndexEntry;

typedef struct TraceBuffer {
    FileTable ** files;         /* FileTables are individually allocated so */
    size_t nfiles;              /* pointers to them stay stable as this */
    size_t files_alloc;         /* array grows. */
    FileTableIndexEntry * index;
    size_t index_mask;          /* Capacity - 1, or 0 when index is NULL. */
} TraceBuffer;

/* One stolen table: a drained snapshot of a FileTable's contents. */
typedef struct StolenTable {
    PyObject * filename;        /* Borrowed from the still-live FileTable. */
    PyObject * plugin_name;     /* Borrowed, or NULL. */
    uint64 * slots;             /* Now owned by the stealer: PyMem_Free it. */
    size_t mask;
    BOOL has_zero;
} StolenTable;

void TraceBuffer_init(TraceBuffer * buf);
void TraceBuffer_dealloc(TraceBuffer * buf);

/* Find or create the FileTable for `filename`.  On creation, references to
 * `filename` and `plugin_name` (which may be NULL) are taken.  Returns NULL
 * with an exception set on memory failure.
 */
FileTable * TraceBuffer_get_file_table(TraceBuffer * buf, PyObject * filename, PyObject * plugin_name);

/* Record one value.  Returns RET_OK or RET_ERROR (exception set). */
int FileTable_record(FileTable * table, uint64 value);

/* Move the contents of every non-empty FileTable into a freshly allocated
 * array of StolenTables, resetting the tables to empty.  Pure C: no Python
 * objects are created and nothing can release the GIL, so under the GIL this
 * is atomic with respect to recording.  The caller owns *pstolen and each
 * stolen slots array.  Returns RET_OK or RET_ERROR (exception set, buffer
 * left unchanged).
 */
int TraceBuffer_steal(TraceBuffer * buf, StolenTable ** pstolen, size_t * pcount);

#endif /* _COVERAGE_BUFFER_H */

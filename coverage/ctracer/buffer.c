/* Licensed under the Apache License: http://www.apache.org/licenses/LICENSE-2.0 */
/* For details: https://github.com/coveragepy/coveragepy/blob/main/NOTICE.txt */

#include "buffer.h"

/* Everything in this file must stay pure C: PyMem allocation and refcount
 * operations only, never anything that can run Python code (object
 * allocation, arbitrary DECREFs, comparisons).  That's what makes each
 * operation atomic under the GIL.  See buffer.h.
 */

#define INITIAL_FILES       8
#define INITIAL_INDEX_CAP   16
#define INITIAL_SLOTS_CAP   32

/* Fibonacci hashing: multiply by 2^64/phi, use the high bits. */
static inline size_t
hash_value(uint64 value)
{
    uint64 h = value * 11400714819323198485ULL;
    return (size_t)(h >> 32) ^ (size_t)h;
}

static inline size_t
hash_pointer(PyObject * ptr)
{
    /* Low bits of a pointer are alignment zeros; mix them away. */
    return hash_value((uint64)(uintptr_t)ptr >> 3);
}

void
TraceBuffer_init(TraceBuffer * buf)
{
    buf->files = NULL;
    buf->nfiles = 0;
    buf->files_alloc = 0;
    buf->index = NULL;
    buf->index_mask = 0;
}

void
TraceBuffer_dealloc(TraceBuffer * buf)
{
    size_t i;

    for (i = 0; i < buf->nfiles; i++) {
        FileTable * table = buf->files[i];
        Py_XDECREF(table->filename);
        Py_XDECREF(table->plugin_name);
        PyMem_Free(table->slots);
        PyMem_Free(table);
    }
    PyMem_Free(buf->files);
    PyMem_Free(buf->index);
    TraceBuffer_init(buf);
}

static void
index_insert(FileTableIndexEntry * index, size_t mask, PyObject * key, size_t idx)
{
    size_t i = hash_pointer(key) & mask;
    while (index[i].key != NULL) {
        i = (i + 1) & mask;
    }
    index[i].key = key;
    index[i].idx = idx;
}

/* Grow (or create) the filename index so it can hold one more entry. */
static int
index_maybe_grow(TraceBuffer * buf)
{
    size_t cap = buf->index ? buf->index_mask + 1 : 0;

    if (buf->index != NULL && (buf->nfiles + 1) * 3 < cap * 2) {
        return RET_OK;
    }

    size_t newcap = cap ? cap * 2 : INITIAL_INDEX_CAP;
    FileTableIndexEntry * newindex = PyMem_Calloc(newcap, sizeof(FileTableIndexEntry));
    if (newindex == NULL) {
        PyErr_NoMemory();
        return RET_ERROR;
    }
    for (size_t i = 0; i < buf->nfiles; i++) {
        index_insert(newindex, newcap - 1, buf->files[i]->filename, i);
    }
    PyMem_Free(buf->index);
    buf->index = newindex;
    buf->index_mask = newcap - 1;
    return RET_OK;
}

FileTable *
TraceBuffer_get_file_table(TraceBuffer * buf, PyObject * filename, PyObject * plugin_name)
{
    /* The common case: this filename already has a table. */
    if (buf->index != NULL) {
        size_t i = hash_pointer(filename) & buf->index_mask;
        while (buf->index[i].key != NULL) {
            if (buf->index[i].key == filename) {
                return buf->files[buf->index[i].idx];
            }
            i = (i + 1) & buf->index_mask;
        }
    }

    /* A new file: make a table for it. */
    if (index_maybe_grow(buf) < 0) {
        return NULL;
    }
    if (buf->nfiles >= buf->files_alloc) {
        size_t newalloc = buf->files_alloc ? buf->files_alloc * 2 : INITIAL_FILES;
        FileTable ** newfiles = PyMem_Realloc(buf->files, newalloc * sizeof(FileTable *));
        if (newfiles == NULL) {
            PyErr_NoMemory();
            return NULL;
        }
        buf->files = newfiles;
        buf->files_alloc = newalloc;
    }

    FileTable * table = PyMem_Malloc(sizeof(FileTable));
    if (table == NULL) {
        PyErr_NoMemory();
        return NULL;
    }
    table->filename = filename;
    Py_INCREF(filename);
    table->plugin_name = plugin_name;
    Py_XINCREF(plugin_name);
    table->slots = NULL;
    table->mask = 0;
    table->used = 0;
    table->has_zero = FALSE;

    buf->files[buf->nfiles] = table;
    index_insert(buf->index, buf->index_mask, filename, buf->nfiles);
    buf->nfiles++;
    return table;
}

static int
FileTable_grow(FileTable * table)
{
    size_t newcap = table->slots ? (table->mask + 1) * 2 : INITIAL_SLOTS_CAP;
    uint64 * newslots = PyMem_Calloc(newcap, sizeof(uint64));
    if (newslots == NULL) {
        PyErr_NoMemory();
        return RET_ERROR;
    }

    if (table->slots != NULL) {
        size_t i;
        for (i = 0; i <= table->mask; i++) {
            uint64 value = table->slots[i];
            if (value != 0) {
                size_t j = hash_value(value) & (newcap - 1);
                while (newslots[j] != 0) {
                    j = (j + 1) & (newcap - 1);
                }
                newslots[j] = value;
            }
        }
        PyMem_Free(table->slots);
    }
    table->slots = newslots;
    table->mask = newcap - 1;
    return RET_OK;
}

int
FileTable_record(FileTable * table, uint64 value)
{
    if (value == 0) {
        table->has_zero = TRUE;
        return RET_OK;
    }

    /* Grow at 2/3 load. */
    if (table->slots == NULL || (table->used + 1) * 3 > (table->mask + 1) * 2) {
        if (FileTable_grow(table) < 0) {
            return RET_ERROR;
        }
    }

    size_t i = hash_value(value) & table->mask;
    while (table->slots[i] != 0) {
        if (table->slots[i] == value) {
            return RET_OK;
        }
        i = (i + 1) & table->mask;
    }
    table->slots[i] = value;
    table->used++;
    return RET_OK;
}

int
TraceBuffer_steal(TraceBuffer * buf, StolenTable ** pstolen, size_t * pcount)
{
    size_t i, count = 0;

    for (i = 0; i < buf->nfiles; i++) {
        FileTable * table = buf->files[i];
        if (table->used > 0 || table->has_zero) {
            count++;
        }
    }

    *pstolen = NULL;
    *pcount = 0;
    if (count == 0) {
        return RET_OK;
    }

    StolenTable * stolen = PyMem_Malloc(count * sizeof(StolenTable));
    if (stolen == NULL) {
        PyErr_NoMemory();
        return RET_ERROR;
    }

    count = 0;
    for (i = 0; i < buf->nfiles; i++) {
        FileTable * table = buf->files[i];
        if (table->used == 0 && !table->has_zero) {
            continue;
        }
        stolen[count].filename = table->filename;
        stolen[count].plugin_name = table->plugin_name;
        stolen[count].slots = table->slots;
        stolen[count].mask = table->mask;
        stolen[count].has_zero = table->has_zero;
        count++;

        /* Reset the table; pointers to it stay valid and it refills lazily. */
        table->slots = NULL;
        table->mask = 0;
        table->used = 0;
        table->has_zero = FALSE;
    }

    *pstolen = stolen;
    *pcount = count;
    return RET_OK;
}

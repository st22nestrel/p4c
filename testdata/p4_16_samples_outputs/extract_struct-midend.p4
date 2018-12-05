#include <core.p4>

header Header {
    bit<32> data;
}

struct Tst {
    bit<32> data;
}

parser p0(packet_in p, out Header h, out Tst t) {
    bool b_0;
    state start {
        b_0 = true;
        p.extract<Header>(h);
        p.extract<Tst>(t);
        transition select(h.data, (bit<1>)b_0) {
            (default, 1w1): next;
            (default, default): reject;
            default: noMatch;
        }
    }
    state next {
        p.extract<Header>(h);
        transition select(h.data, (bit<1>)b_0) {
            (default, 1w1): accept;
            (default, default): reject;
            default: reject;
        }
    }
    state noMatch {
        verify(false, error.NoMatch);
        transition reject;
    }
}

parser proto(packet_in p, out Header h, out Tst t);
package top(proto _p);
top(p0()) main;

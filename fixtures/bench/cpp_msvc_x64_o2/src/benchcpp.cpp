// benchcpp: shape calculator CLI with classes, virtual dispatch (vtables), RTTI, exceptions and templates.
// Used as R0 benchmark ground truth (Rebuild Studio fixtures/bench). Written for this repository.
// usage: benchcpp <spec>...   spec = circle:R | rect:W:H | tri:A:B:C | square:S
// Exit codes: 0 ok, 1 usage, 2 invalid shape (exception path).
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

#if defined(_MSC_VER)
#define NOINLINE __declspec(noinline)
#else
#define NOINLINE __attribute__((noinline))
#endif

namespace bench {

class ShapeError : public std::runtime_error {
public:
    explicit ShapeError(const std::string &what) : std::runtime_error(what) {}
};

class Shape {
public:
    virtual ~Shape() = default;
    virtual double area() const = 0;
    virtual double perimeter() const = 0;
    virtual const char *kind() const = 0;
    NOINLINE virtual std::string describe() const {
        char buf[128];
        std::snprintf(buf, sizeof buf, "%s area=%.3f perimeter=%.3f", kind(), area(), perimeter());
        return buf;
    }
};

class Circle : public Shape {
public:
    NOINLINE explicit Circle(double r) : r_(r) {
        if (r <= 0) throw ShapeError("circle radius must be positive");
    }
    NOINLINE double area() const override { return 3.14159265358979 * r_ * r_; }
    NOINLINE double perimeter() const override { return 2 * 3.14159265358979 * r_; }
    const char *kind() const override { return "circle"; }
private:
    double r_;
};

class Rect : public Shape {
public:
    NOINLINE Rect(double w, double h) : w_(w), h_(h) {
        if (w <= 0 || h <= 0) throw ShapeError("rectangle sides must be positive");
    }
    NOINLINE double area() const override { return w_ * h_; }
    NOINLINE double perimeter() const override { return 2 * (w_ + h_); }
    const char *kind() const override { return "rect"; }
protected:
    double w_, h_;
};

class Square final : public Rect {
public:
    NOINLINE explicit Square(double s) : Rect(s, s) {}
    const char *kind() const override { return "square"; }
    NOINLINE std::string describe() const override { return "square side=" + std::to_string(w_) + " " + Rect::describe(); }
};

class Triangle : public Shape {
public:
    NOINLINE Triangle(double a, double b, double c) : a_(a), b_(b), c_(c) {
        if (a <= 0 || b <= 0 || c <= 0 || a + b <= c || a + c <= b || b + c <= a)
            throw ShapeError("triangle inequality violated");
    }
    NOINLINE double area() const override {
        double s = (a_ + b_ + c_) / 2;
        return std::sqrt(s * (s - a_) * (s - b_) * (s - c_));
    }
    NOINLINE double perimeter() const override { return a_ + b_ + c_; }
    const char *kind() const override { return "tri"; }
private:
    double a_, b_, c_;
};

template <typename T>
class Stats {
public:
    NOINLINE void add(T v) {
        values_.push_back(v);
        sum_ += v;
    }
    NOINLINE T mean() const { return values_.empty() ? T() : sum_ / static_cast<T>(values_.size()); }
    NOINLINE T maximum() const {
        T m = values_.empty() ? T() : values_[0];
        for (const T &v : values_) if (v > m) m = v;
        return m;
    }
    size_t count() const { return values_.size(); }
private:
    std::vector<T> values_;
    T sum_ = T();
};

NOINLINE std::vector<double> parse_numbers(const std::string &spec, size_t first) {
    std::vector<double> out;
    size_t pos = first;
    while (pos < spec.size()) {
        size_t next = spec.find(':', pos);
        std::string part = spec.substr(pos, next == std::string::npos ? std::string::npos : next - pos);
        char *end = nullptr;
        double v = std::strtod(part.c_str(), &end);
        if (part.empty() || *end) throw ShapeError("bad number '" + part + "'");
        out.push_back(v);
        if (next == std::string::npos) break;
        pos = next + 1;
    }
    return out;
}

NOINLINE std::unique_ptr<Shape> make_shape(const std::string &spec) {
    size_t colon = spec.find(':');
    if (colon == std::string::npos) throw ShapeError("missing ':' in '" + spec + "'");
    std::string name = spec.substr(0, colon);
    std::vector<double> n = parse_numbers(spec, colon + 1);
    if (name == "circle" && n.size() == 1) return std::make_unique<Circle>(n[0]);
    if (name == "rect" && n.size() == 2) return std::make_unique<Rect>(n[0], n[1]);
    if (name == "square" && n.size() == 1) return std::make_unique<Square>(n[0]);
    if (name == "tri" && n.size() == 3) return std::make_unique<Triangle>(n[0], n[1], n[2]);
    throw ShapeError("unknown shape '" + name + "'");
}

NOINLINE void print_summary(const std::map<std::string, int> &by_kind, const Stats<double> &areas) {
    std::printf("shapes=%zu mean_area=%.3f max_area=%.3f\n", areas.count(), areas.mean(), areas.maximum());
    for (const auto &kv : by_kind) std::printf("  %-6s x%d\n", kv.first.c_str(), kv.second);
}

}  // namespace bench

int main(int argc, char **argv) {
    if (argc < 2) {
        std::fputs("benchcpp 1.0 - Rebuild Studio benchmark fixture\nusage: benchcpp circle:R | rect:W:H | tri:A:B:C | square:S ...\n", stderr);
        return 1;
    }
    std::vector<std::unique_ptr<bench::Shape>> shapes;
    std::map<std::string, int> by_kind;
    bench::Stats<double> areas;
    try {
        for (int i = 1; i < argc; i++) {
            shapes.push_back(bench::make_shape(argv[i]));
        }
    } catch (const bench::ShapeError &e) {
        std::fprintf(stderr, "benchcpp: invalid shape: %s\n", e.what());
        return 2;
    }
    for (const auto &s : shapes) {
        std::puts(s->describe().c_str());
        by_kind[s->kind()]++;
        areas.add(s->area());
        if (const auto *sq = dynamic_cast<const bench::Square *>(s.get())) {
            std::printf("  (square perimeter %.1f)\n", sq->perimeter());
        }
    }
    bench::print_summary(by_kind, areas);
    return 0;
}

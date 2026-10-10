// benchgo: inventory CLI used as R0 benchmark ground truth (Rebuild Studio fixtures/bench). Written for this repository.
// usage: benchgo <add|total|top|json> name=qty@price ...
// Exit codes: 0 ok, 1 usage, 2 parse error.
package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"sort"
	"strconv"
	"strings"
)

// Item is one inventory line.
type Item struct {
	Name  string  `json:"name"`
	Qty   int     `json:"qty"`
	Price float64 `json:"price"`
}

// Inventory keeps items by name.
type Inventory struct {
	items map[string]*Item
}

var errBadSpec = errors.New("benchgo: spec must look like name=qty@price")

const usageText = "benchgo 1.0 - Rebuild Studio benchmark fixture\nusage: benchgo <add|total|top|json> name=qty@price ...\n"

//go:noinline
func newInventory() *Inventory {
	return &Inventory{items: make(map[string]*Item)}
}

//go:noinline
func parseSpec(spec string) (Item, error) {
	eq := strings.IndexByte(spec, '=')
	at := strings.IndexByte(spec, '@')
	if eq <= 0 || at < eq {
		return Item{}, errBadSpec
	}
	qty, err := strconv.Atoi(spec[eq+1 : at])
	if err != nil {
		return Item{}, fmt.Errorf("benchgo: bad quantity in %q: %w", spec, err)
	}
	price, err := strconv.ParseFloat(spec[at+1:], 64)
	if err != nil {
		return Item{}, fmt.Errorf("benchgo: bad price in %q: %w", spec, err)
	}
	return Item{Name: spec[:eq], Qty: qty, Price: price}, nil
}

//go:noinline
func (inv *Inventory) Add(it Item) {
	if cur, ok := inv.items[it.Name]; ok {
		cur.Qty += it.Qty
		cur.Price = it.Price
		return
	}
	c := it
	inv.items[it.Name] = &c
}

//go:noinline
func (inv *Inventory) Total() float64 {
	t := 0.0
	for _, it := range inv.items {
		t += float64(it.Qty) * it.Price
	}
	return t
}

//go:noinline
func (inv *Inventory) Sorted() []Item {
	out := make([]Item, 0, len(inv.items))
	for _, it := range inv.items {
		out = append(out, *it)
	}
	sort.Slice(out, func(i, j int) bool {
		vi, vj := float64(out[i].Qty)*out[i].Price, float64(out[j].Qty)*out[j].Price
		if vi != vj {
			return vi > vj
		}
		return out[i].Name < out[j].Name
	})
	return out
}

//go:noinline
func renderTop(items []Item, n int) string {
	var b strings.Builder
	for i, it := range items {
		if i >= n {
			break
		}
		fmt.Fprintf(&b, "%2d. %-12s qty=%-4d value=%.2f\n", i+1, it.Name, it.Qty, float64(it.Qty)*it.Price)
	}
	return b.String()
}

//go:noinline
func run(args []string) int {
	if len(args) < 1 {
		fmt.Fprint(os.Stderr, usageText)
		return 1
	}
	inv := newInventory()
	for _, spec := range args[1:] {
		it, err := parseSpec(spec)
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			return 2
		}
		inv.Add(it)
	}
	switch args[0] {
	case "add":
		fmt.Printf("added %d distinct items\n", len(inv.items))
	case "total":
		fmt.Printf("total value: %.2f\n", inv.Total())
	case "top":
		fmt.Print(renderTop(inv.Sorted(), 3))
	case "json":
		data, err := json.MarshalIndent(inv.Sorted(), "", "  ")
		if err != nil {
			return 2
		}
		fmt.Println(string(data))
	default:
		fmt.Fprint(os.Stderr, usageText)
		return 1
	}
	return 0
}

func main() {
	os.Exit(run(os.Args[1:]))
}

let cart = [];


function updateCartCount() {
    let count = 0;

    for (let item of cart) {
        count = count + item.quantity;
    }

    document.getElementById("cart-count").textContent = count;
    document.getElementById("nav-cart-count").textContent = count;
}


function addToCart(productName, productPrice) {

    console.log(productName);
    console.log(productPrice);

    let existingProduct = cart.find(function(item) {
    return item.name === productName;
    });

        if (existingProduct) {
        existingProduct.quantity++;
        } 
    else {
        cart.push({
            name: productName,
            price: productPrice,
            quantity: 1
        });
    }

    localStorage.setItem("cart", JSON.stringify(cart));

    updateCartCount();

    displayCart();
}

function displayCart() {
    let cartItems = document.getElementById("cart-items");

    cartItems.innerHTML = "";

    let total = 0;

    for (let item of cart) {
        let product = document.createElement("div");

        product.className = "cart-item";

        let productInfo = document.createElement("span");

        productInfo.textContent =
            item.name + " - ₹" + item.price;

        product.appendChild(productInfo);

        let quantityInfo = document.createElement("span");

        quantityInfo.textContent = " Quantity: " + item.quantity;


        

       

         let removeButton = document.createElement("button");
         removeButton.textContent = "Remove";
         removeButton.className = "remove-button";

         let increaseButton = document.createElement("button");
         increaseButton.textContent = "+";

         let decreaseButton = document.createElement("button");
         decreaseButton.textContent = "-";

         let quantityControls = document.createElement("div");
         quantityControls.className = "quantity-controls";

         decreaseButton.onclick = function() {
    if (item.quantity > 1) {
        item.quantity--;
localStorage.setItem("cart", JSON.stringify(cart));

        updateCartCount();
        displayCart();
            }
        };

        increaseButton.onclick = function() {
        item.quantity++;
 localStorage.setItem("cart", JSON.stringify(cart));
        updateCartCount();
        displayCart();
    };

        removeButton.onclick = function() {
    cart = cart.filter(function(cartItem) {
        return cartItem !== item;
    });

    localStorage.setItem("cart", JSON.stringify(cart));

        updateCartCount();
        displayCart();
    };
       
        quantityControls.appendChild(decreaseButton);
        quantityControls.appendChild(quantityInfo);
        quantityControls.appendChild(increaseButton);
        quantityControls.appendChild(removeButton);

        product.appendChild(quantityControls);

        cartItems.appendChild(product);

        total = total + (item.price * item.quantity);
    }

    document.getElementById("cart-total").textContent = "Total: ₹" + total;
}

let savedCart = localStorage.getItem("cart");

if (savedCart) {
    cart = JSON.parse(savedCart);
}

updateCartCount();
displayCart();

document.getElementById("checkout-button").onclick = function() {
    alert("Checkout clicked!");
};
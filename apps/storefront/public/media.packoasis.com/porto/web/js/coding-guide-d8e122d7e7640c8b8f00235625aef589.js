document.addEventListener("DOMContentLoaded", function () {
    requirejs([
        'jquery',
        '//unpkg.com/material-components-web@v4.0.0/dist/material-components-web.min.js'
    ], function ($, mdc) {
        "use strict";
        
        /* ===== Google Compopent Material Design. Scrolling tabs ===== */

        if($('.custom-material-tabs-container').length) {
            // main instance for tabbar
            var tabBar = new mdc.tabBar.MDCTabBar(document.querySelector('.mdc-tab-bar'));
            var contentEls = $('.custom-materials-tab-content');
            tabBar.listen('MDCTabBar:activated', function (event) {
                // Hide currently-active content
                contentEls.removeClass('active');
                // Show content for newly-activated tab
                contentEls[event.detail.index].classList.add('active');
            });
        }
    });
});

